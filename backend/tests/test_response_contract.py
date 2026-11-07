"""Response-contract guards for stateless API/agent callers (2026-11-07).

Most real traffic is stateless (bots/scripts that never send conversationId
back), so three response-contract mechanisms are pinned here:

1. Clarification options carry a self-contained ``query`` re-ask string that is
   verified at build time to round-trip through the EXISTING exact
   provider-code fast path to the same provider + code + country scope (no new
   parser), and the strings are rendered in text a bot reads.
2. When the user explicitly named a provider, that provider's options lead
   the indicator-choice menu (stable partition before truncation); other
   providers' options remain as disclosed alternatives.
3. A partial multi-indicator answer discloses the unserved indicators in the
   response contract (``message`` + structured ``unservedIndicators``) through
   a structured channel, not by scanning metadata.notes.

Codes used below are stub fixtures / round-trip subjects; assertions are about
mechanism behaviour, never series-id pins for a natural-language query.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from backend.models import (
    ClarificationOption,
    Metadata,
    NormalizedData,
    ParsedIntent,
    QueryResponse,
)
from backend.routing.country_resolver import CountryResolver
from backend.services import data_fetcher as DF
from backend.services import indicator_clarification as clarification
from backend.services.indicator_resolution import is_exact_match_locked
from backend.services.indicator_selector import IndicatorSelector
from backend.services.provider_strategy import collect_target_countries
from backend.services.query import QueryService
from backend.tests.utils import run
from backend.utils.retry import DataNotAvailableError


@pytest.fixture(scope="module")
def service() -> QueryService:
    return QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")


def _intent(provider: str, params: dict, query: str = "q", indicators=None) -> ParsedIntent:
    return ParsedIntent(
        apiProvider=provider,
        indicators=list(indicators or ["indicator"]),
        parameters=dict(params),
        clarificationNeeded=False,
        originalQuery=query,
    )


def _option(provider: str, code: str, idx: int = 1) -> ClarificationOption:
    return ClarificationOption(
        id=str(idx), label="x", value=f"[{provider}] x ({code})", provider=provider, code=code,
    )


# ---------------------------------------------------------------------------
# (1) Stateless re-ask strings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "provider,code,params,expected_iso2",
    [
        ("FRED", "M2SL", {"country": "US"}, {"US"}),
        ("WORLDBANK", "NY.GDP.MKTP.CD", {"country": "Germany"}, {"DE"}),
        ("BIS", "BIS_WS_TC", {"country": "Japan"}, {"JP"}),
        ("IMF", "NGDP_RPCH", {"country": "United States"}, {"US"}),
        ("EUROSTAT", "une_rt_m", {"country": "Germany"}, {"DE"}),
        ("FRED", "M2SL", {}, set()),
    ],
)
def test_option_query_round_trips_to_exact_locked_same_series(
    service, provider, code, params, expected_iso2,
):
    option = _option(provider, code)
    query = clarification.stateless_option_query(service, option, _intent(provider, params))

    assert query, "a single-country, undated option must carry a re-ask string"
    assert "[" not in query, "bracket-prefix strings do not resolve through the fast path"
    # Re-issuing the string resolves authoritatively to exactly this series —
    # an exact-match-locked intent never re-enters indicator clarification.
    resolved = service._build_explicit_provider_code_intent(query)  # pylint: disable=protected-access
    assert resolved is not None
    assert resolved.apiProvider == provider
    assert str(resolved.parameters["indicator"]).upper() == code.upper()
    assert is_exact_match_locked(resolved.parameters)
    assert resolved.clarificationNeeded is False
    resolved_countries = (
        [resolved.parameters["country"]] if resolved.parameters.get("country")
        else list(resolved.parameters.get("countries") or [])
    )
    assert {CountryResolver.normalize(c) for c in resolved_countries} == expected_iso2


@pytest.mark.parametrize(
    "params",
    [
        # Multi-country scope cannot be carried by today's fast path: emitting
        # a single-country string would silently drop countries.
        {"countries": ["US", "GB", "JP"]},
        # A USER-set window cannot be carried either: never drop it silently.
        {"country": "US", "startDate": "2020-01-01", "endDate": "2026-12-31",
         "__time_scope_authority": "user"},
        # A user window that cannot be rendered as a year range ("since 2010"
        # with no end) must not degrade to a dateless string either.
        {"country": "US", "startDate": "2010-01-01", "__time_scope_authority": "user"},
        # An unresolvable scope member cannot be verified.
        {"country": "Atlantis"},
    ],
)
def test_option_query_is_omitted_when_scope_cannot_round_trip(service, params):
    option = _option("BIS", "BIS_WS_TC")
    assert clarification.stateless_option_query(service, option, _intent("BIS", params)) is None


def test_default_time_window_is_not_presented_as_user_scope(service):
    params = {"country": "US", "startDate": "2016-01-01", "endDate": "2026-12-31",
              "__time_scope_authority": "default"}
    query = clarification.stateless_option_query(service, _option("FRED", "M2SL"), _intent("FRED", params))
    assert query is not None
    assert "2016" not in query and "2026" not in query


def test_option_without_provider_code_gets_no_query(service):
    option = ClarificationOption(id="1", label="compare member countries", value="GDP for EU members")
    assert clarification.stateless_option_query(service, option, _intent("FRED", {})) is None


def test_round_trip_gate_rejects_string_resolving_elsewhere():
    """The gate, not the formatter, decides: a mismatched resolution is never emitted."""
    resolved = _intent("FRED", {"indicator": "OTHER", "__exact_provider_code_match": True,
                                "country": "US"})
    qs = SimpleNamespace(
        _collect_target_countries=collect_target_countries,
        _normalize_country_to_iso2=CountryResolver.normalize,
        _build_explicit_provider_code_intent=lambda text: resolved,
    )
    assert clarification.stateless_option_query(qs, _option("FRED", "M2SL"), _intent("FRED", {"country": "US"})) is None
    # Same code but NOT exact-locked -> still rejected.
    resolved.parameters = {"indicator": "M2SL", "country": "US"}
    assert clarification.stateless_option_query(qs, _option("FRED", "M2SL"), _intent("FRED", {"country": "US"})) is None


def _menu_response(options, questions, intent, message=None) -> QueryResponse:
    return QueryResponse(
        conversationId="conv-1",
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=list(questions),
        clarificationOptions=options,
        message=message,
    )


def test_contract_renders_requeries_in_browser_hidden_reply_line_and_message(service):
    intent = _intent("FRED", {"country": "US"}, query="US money supply")
    options = [_option("FRED", "M2SL", 1), _option("BIS", "BIS_WS_TC", 2)]
    questions = [
        "Please choose the indicator you intended:",
        "1. [FRED] x (M2SL)",
        "2. [BIS] x (BIS_WS_TC)",
        "Reply with the option number (for example, 1) or the exact indicator text you want.",
    ]
    response = clarification.finalize_clarification_response_contract(
        service, _menu_response(options, questions, intent),
    )

    assert [o.query for o in response.clarificationOptions] == [
        "US M2SL from FRED", "US BIS_WS_TC from BIS",
    ]
    # No new free-standing lines: the existing reply line (which the browser
    # sanitizer hides) carries conversationId + the re-ask strings.
    assert len(response.clarificationQuestions) == len(questions)
    reply_line = response.clarificationQuestions[-1]
    assert reply_line.lower().startswith("reply with the option number")
    assert 'conversationId="conv-1"' in reply_line
    assert "1) US M2SL from FRED" in reply_line and "2) US BIS_WS_TC from BIS" in reply_line
    # Clients that read only `message` see the full menu.
    assert response.message == "\n".join(response.clarificationQuestions)

    # Idempotent.
    again = clarification.finalize_clarification_response_contract(service, response)
    assert again.clarificationQuestions == response.clarificationQuestions


def test_contract_adds_hidden_reply_line_when_builder_had_none(service):
    intent = _intent("FRED", {"country": "US"})
    options = [_option("FRED", "M2SL", 1), _option("FRED", "M1SL", 2)]
    response = clarification.finalize_clarification_response_contract(
        service, _menu_response(options, ["Which money supply measure?"], intent, message="kept"),
    )
    assert response.clarificationQuestions[0] == "Which money supply measure?"
    assert response.clarificationQuestions[-1].lower().startswith("reply with the option number")
    assert response.message == "kept", "an existing message is never overwritten"


def test_contract_ignores_non_clarification_responses(service):
    response = QueryResponse(conversationId="c", clarificationNeeded=False, message=None)
    assert clarification.finalize_clarification_response_contract(service, response).message is None


# ---------------------------------------------------------------------------
# (2) Explicit-provider menu ordering
# ---------------------------------------------------------------------------

WB_CANDIDATES = [
    (f"FS.AST.PRVT.GD.Z{i}", f"Domestic credit to private sector variant {i} (% of GDP)") for i in range(5)
]
BIS_CANDIDATES = [
    ("BIS_WS_TC", "Total credit to the private non-financial sector"),
    ("BIS_WS_TC_GDP", "Total credit to the non-financial sector (% of GDP)"),
]


@pytest.fixture
def collect(monkeypatch):
    monkeypatch.setattr(clarification, "_score_series_relevance", lambda *args: 0.0)
    monkeypatch.setattr(clarification, "option_supportability_reason", lambda *args: None)
    monkeypatch.setattr(
        "backend.services.provider_fallback.provider_covers_country_list",
        lambda *args, **kwargs: True,
    )
    by_provider = {
        "WORLDBANK": (WB_CANDIDATES, [0.99, 0.98, 0.97, 0.96, 0.95]),
        "BIS": (BIS_CANDIDATES, [0.60, 0.55]),
    }
    monkeypatch.setattr(
        IndicatorSelector, "_get_candidates_with_scores",
        lambda self, query, current_provider, **kwargs: copy.deepcopy(
            by_provider.get(current_provider, ([], []))
        ),
    )

    def run_collect(query: str, explicit_provider, max_options: int = 4):
        qs = SimpleNamespace(
            _select_indicator_query_for_resolution=lambda intent: "credit to GDP ratio",
            _collect_target_countries=collect_target_countries,
            _extract_countries_from_query=lambda q: [],
            _normalize_country_to_iso2=CountryResolver.normalize,
            _get_fallback_providers=lambda *args, **kwargs: ["WORLDBANK"],
            _detect_explicit_provider=lambda q: explicit_provider,
            bis_provider=SimpleNamespace(_lookup_dataflow_info=lambda code: (None, None)),
        )
        intent = _intent("BIS", {"country": "US"}, query=query, indicators=["credit to GDP ratio"])
        options = clarification.collect_indicator_choice_options(qs, query, intent, max_options=max_options)
        return [clarification.parse_indicator_option(option)[0] for option in options]

    return run_collect


def test_explicit_provider_options_lead_and_survive_truncation(collect):
    providers = collect("Credit to GDP ratio US from BIS", explicit_provider="BIS", max_options=4)
    assert providers[:2] == ["BIS", "BIS"]
    # Alternatives are kept after the named provider (re-rank only, size unchanged).
    assert providers[2:] == ["WORLDBANK", "WORLDBANK"]


def test_no_explicit_provider_keeps_score_ordering(collect):
    providers = collect("Credit to GDP ratio US", explicit_provider=None, max_options=4)
    # Pure score order: the higher-scoring WorldBank candidates fill the menu.
    assert providers == ["WORLDBANK"] * 4


def test_explicit_provider_with_no_options_keeps_alternatives_and_discloses(service, collect):
    providers = collect("Credit to GDP ratio US from FRED", explicit_provider="FRED", max_options=3)
    assert providers == ["WORLDBANK"] * 3, "never an empty menu just because the named provider has nothing"

    intent = _intent("FRED", {"country": "US"}, query="Credit to GDP ratio US from FRED")
    options = [_option("WORLDBANK", code, idx) for idx, (code, _) in enumerate(WB_CANDIDATES[:2], start=1)]
    response = clarification.finalize_clarification_response_contract(
        service, _menu_response(options, ["1. a", "2. b"], intent),
    )
    assert response.clarificationQuestions[0].startswith("FRED has no matching series at this scope")


def test_mixed_menu_labels_other_provider_options_as_alternatives(service):
    intent = _intent("BIS", {"country": "US"}, query="Credit to GDP ratio US from BIS")
    options = [_option("BIS", "BIS_WS_TC", 1), _option("WORLDBANK", "FS.AST.PRVT.GD.ZS", 2)]
    response = clarification.finalize_clarification_response_contract(
        service, _menu_response(options, ["Choose:", "1. a", "2. b"], intent),
    )
    assert response.clarificationQuestions[1] == "Option(s) 2 are alternatives from providers other than BIS."
    # Option text/value keep the parseable "[Provider] ... (CODE)" shape.
    assert [clarification.parse_indicator_option(o.value) for o in response.clarificationOptions] == [
        ("BIS", "BIS_WS_TC"), ("WORLDBANK", "FS.AST.PRVT.GD.ZS"),
    ]


def test_no_data_menu_excludes_the_code_that_just_returned_nothing():
    options = [
        "[BIS] Total credit (BIS_WS_TC)",
        "[BIS] Credit-to-GDP gap (BIS_WS_CREDIT_GAP)",
        "[WorldBank] Domestic credit (FS.AST.PRVT.GD.ZS)",
    ]
    qs = SimpleNamespace(_collect_indicator_choice_options=lambda **kwargs: list(options))
    intent = _intent("BIS", {"country": "US", "indicator": "BIS_WS_TC"}, query="credit from BIS")
    with patch.object(clarification, "dedupe_indicator_choice_options", side_effect=lambda qs, opts: list(opts)), \
         patch.object(clarification, "store_pending_indicator_options"), \
         patch.object(clarification, "build_clarification_options", return_value=None):
        response = clarification.build_no_data_indicator_clarification(qs, "c", "credit from BIS", intent)
    assert response is not None
    assert not any("BIS_WS_TC)" in line for line in response.clarificationQuestions)
    assert any("BIS_WS_CREDIT_GAP" in line for line in response.clarificationQuestions)


# ---------------------------------------------------------------------------
# (3) Partial multi-indicator disclosure
# ---------------------------------------------------------------------------

def _series(indicator: str, source: str = "FRED") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country="US", frequency="monthly",
            unit="Percent", seriesId=indicator.upper(),
        ),
        data=[{"date": "2020-01-01", "value": 1.0}],
    )


def _fetch_svc() -> Mock:
    svc = Mock()
    svc._detect_explicit_provider = Mock(return_value="")
    svc._normalize_provider_alias = Mock(side_effect=lambda x: x or None)
    svc._select_routed_provider = AsyncMock(return_value="FRED")
    return svc


def _fake_fetch(failing: str):
    async def _inner(_svc, single_intent):
        name = single_intent.indicators[0]
        if failing and failing.lower() in name.lower():
            raise DataNotAvailableError(f"no data for {name}")
        return [_series(name)]

    return _inner


def _process_with_multi_fetch(service, indicators, failing, language=None, serve_other=False):
    intent = _intent("FRED", {"country": "US"}, query="US unemployment and CPI", indicators=indicators)
    if language:
        intent.language = language

    async def _impl(query, **kwargs):
        data = await DF.fetch_multi_indicator_data(_fetch_svc(), intent)
        if serve_other:  # a later path replaced the partial data entirely
            data = [_series("replacement", source="WORLDBANK")]
        return QueryResponse(conversationId="c", intent=intent, data=data, clarificationNeeded=False)

    with patch.object(DF, "fetch_data", new=_fake_fetch(failing)), \
         patch.object(service, "_process_query_impl", new=_impl):
        return run(service.process_query("US unemployment and CPI"))


def test_partial_multi_indicator_answer_discloses_unserved_in_message(service):
    response = _process_with_multi_fetch(service, ["unemployment rate", "CPI"], failing="CPI")

    assert [s.metadata.indicator for s in response.data] == ["unemployment rate"]
    assert response.message and "CPI" in response.message
    assert [(u.indicator, u.reason) for u in response.unservedIndicators] == [("CPI", "no_data")]
    # The existing per-series note stays for backward compatibility.
    assert any("CPI" in note for note in response.data[0].metadata.notes or [])


def test_full_multi_indicator_answer_is_unchanged(service):
    response = _process_with_multi_fetch(service, ["unemployment rate", "CPI"], failing="")
    assert len(response.data) == 2
    assert response.message is None
    assert response.unservedIndicators is None


def test_stale_partial_report_is_not_announced_when_data_was_replaced(service):
    response = _process_with_multi_fetch(
        service, ["unemployment rate", "CPI"], failing="CPI", serve_other=True,
    )
    assert response.message is None
    assert response.unservedIndicators is None


def test_partial_disclosure_is_localized(service):
    response = _process_with_multi_fetch(service, ["失业率", "CPI"], failing="CPI", language="zh")
    assert response.message and "无法获取" in response.message


def test_disclosure_appends_to_existing_coverage_warning(service):
    intent = _intent("FRED", {"country": "US"}, indicators=["a", "b"])
    response = QueryResponse(
        conversationId="c", intent=intent, data=[_series("a")], clarificationNeeded=False,
        message="coverage warning",
    )
    token = DF.MULTI_INDICATOR_FETCH_REPORTS.set([
        {"unserved": [{"indicator": "b", "reason": "timeout"}],
         "served": {DF.served_series_identity(response.data[0])}},
    ])
    try:
        service._finalize_response_contract(response)  # pylint: disable=protected-access
        service._finalize_response_contract(response)  # idempotent
    finally:
        DF.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    assert response.message.startswith("coverage warning")
    assert response.message.count("Could not fetch data for: b") == 1
    assert response.unservedIndicators[0].reason == "timeout"


def test_no_report_recorded_outside_a_request():
    intent = _intent("FRED", {"country": "US"}, indicators=["unemployment rate", "CPI"])
    assert DF.MULTI_INDICATOR_FETCH_REPORTS.get() is None
    with patch.object(DF, "fetch_data", new=_fake_fetch("CPI")):
        data = run(DF.fetch_multi_indicator_data(_fetch_svc(), intent))
    assert len(data) == 1
    assert DF.MULTI_INDICATOR_FETCH_REPORTS.get() is None
