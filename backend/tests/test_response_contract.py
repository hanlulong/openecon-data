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
from backend.services.user_messages import CLARIFY_REPLY_HINT_PREFIXES, _MESSAGES, reply_hint_language
from backend.utils.retry import DataNotAvailableError, ProviderCoolingDown


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
    # The contract leaves `message` to the empty-data finalizer's renderer,
    # which shows message-only clients the finalized lines plus every option.
    assert response.message is None
    rendered = service._finalize_empty_data_response(response)  # pylint: disable=protected-access
    message_lines = rendered.message.splitlines()
    assert message_lines[: len(questions)] == response.clarificationQuestions
    assert "1) US M2SL from FRED" in message_lines[len(questions) - 1]

    # Idempotent.
    again = clarification.finalize_clarification_response_contract(service, response)
    assert again.clarificationQuestions == response.clarificationQuestions


def _requery_menu(conversation_id: str, language=None, with_hint=True):
    intent = _intent("FRED", {"country": "US"}, query="US money supply")
    if language:
        intent.language = language
    options = [_option("FRED", "M2SL", 1), _option("BIS", "BIS_WS_TC", 2)]
    hint = _MESSAGES["clarify_reply_hint"][language or "en"]
    questions = ["Choose:", "1. [FRED] x (M2SL)", "2. [BIS] x (BIS_WS_TC)"] + ([hint] if with_hint else [])
    response = _menu_response(options, questions, intent)
    response.conversationId = conversation_id
    return response


def _requery_marker(language=None) -> str:
    template = _MESSAGES["clarify_stateless_requeries"][language or "en"]
    return template.split("{queries}", 1)[0]


@pytest.mark.parametrize("language", [None, "zh"])
@pytest.mark.parametrize("with_hint", [True, False])
@pytest.mark.parametrize("conversation_id", ["", "conv-9"])
def test_contract_is_idempotent_with_or_without_a_conversation_id(service, language, with_hint, conversation_id):
    response = clarification.finalize_clarification_response_contract(
        service, _requery_menu(conversation_id, language, with_hint),
    )
    first = list(response.clarificationQuestions)
    for _ in range(2):
        response = clarification.finalize_clarification_response_contract(service, response)
    assert response.clarificationQuestions == first
    joined = "\n".join(first)
    assert joined.count(_requery_marker(language)) == 1
    assert joined.lower().count("conversationid=") == (1 if conversation_id else 0)
    assert sum(1 for line in first if reply_hint_language(line)) == 1


def test_contract_adds_only_the_missing_resend_part_once_a_conversation_id_appears(service):
    response = clarification.finalize_clarification_response_contract(service, _requery_menu(""))
    response.conversationId = "conv-late"
    response = clarification.finalize_clarification_response_contract(service, response)
    response = clarification.finalize_clarification_response_contract(service, response)
    joined = "\n".join(response.clarificationQuestions)
    assert joined.count(_requery_marker()) == 1
    assert joined.count('conversationId="conv-late"') == 1


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


def test_option_query_is_omitted_when_menu_scope_is_unknown_or_subnational(service):
    option = _option("WORLDBANK", "NY.GDP.MKTP.CD")
    # A menu with no intent (e.g. the pending re-prompt) has an unknown scope:
    # a country-less string would serve the provider default and drop it.
    assert clarification.stateless_option_query(service, option, None) is None
    # A sub-national menu: a national re-ask would skip the fail-closed check
    # a stateful pick still runs.
    intent = _intent("WORLDBANK", {"country": "Canada"})
    intent.subnationalRegion = "Ontario"
    assert clarification.stateless_option_query(service, option, intent) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"frequency": "quarterly"},
        {"dimensions": {"sex": "F"}},
        {"aggregation_method": "avg"},
        {"partner": "China"},
    ],
)
def test_option_query_is_omitted_when_user_scope_cannot_be_carried(service, extra):
    # The fast path re-asks at the series' default form; any user scope it
    # does not reproduce means no string rather than a silent scope drop.
    params = {"country": "US", **extra}
    assert clarification.stateless_option_query(service, _option("FRED", "UNRATE"), _intent("FRED", params)) is None


def test_null_valued_parse_slots_do_not_block_the_option_query(service):
    # Parse intents carry many empty slots (live shape); only VALUES are scope.
    params = {"country": "Japan", "seriesId": None, "reporter": None, "partner": None,
              "flow": None, "coinIds": None, "vsCurrency": None, "indicator": "credit to GDP ratio",
              "__time_scope_authority": "default"}
    assert clarification.stateless_option_query(
        service, _option("BIS", "BIS_WS_TC"), _intent("BIS", params),
    ) == "Japan BIS_WS_TC from BIS"


def test_option_query_uses_country_named_only_in_the_original_query(service):
    intent = _intent("WORLDBANK", {}, query="Germany GDP")
    query = clarification.stateless_option_query(service, _option("WORLDBANK", "NY.GDP.MKTP.CD"), intent)
    assert query, "the collector's scope (countries named in the query) is the menu's scope"
    resolved = service._build_explicit_provider_code_intent(query)  # pylint: disable=protected-access
    assert CountryResolver.normalize(resolved.parameters["country"]) == "DE"


def test_option_query_dedupes_country_aliases_by_iso2(service):
    # Two spellings of one country are one-country scope, not a list the
    # fast path cannot carry.
    params = {"country": "US", "countries": ["United States"]}
    query = clarification.stateless_option_query(service, _option("FRED", "M2SL"), _intent("FRED", params))
    assert query in {"US M2SL from FRED", "United States M2SL from FRED"}


def test_intentless_menu_still_gets_conversation_instruction(service):
    options = [_option("WORLDBANK", "NY.GDP.MKTP.CD", 1), _option("WORLDBANK", "NY.GDP.MKTP.KD", 2)]
    response = clarification.finalize_clarification_response_contract(
        service, _menu_response(options, ["1. a", "2. b"], None),
    )
    assert [o.query for o in response.clarificationOptions] == [None, None]
    assert 'conversationId="conv-1"' in response.clarificationQuestions[-1]
    assert "self-contained" not in response.clarificationQuestions[-1]


ZH_REPLY_HINT = _MESSAGES["clarify_reply_hint"]["zh"]


def _zh_menu(service, intent, options, questions):
    return clarification.finalize_clarification_response_contract(
        service, _menu_response(options, questions, intent),
    )


def test_zh_menu_chrome_stays_in_chinese_and_extends_the_zh_hint(service):
    intent = _intent("IMF", {"country": "China"}, query="中国CPI 来自IMF")
    intent.language = "zh"
    options = [_option("IMF", "PCPI_IX", 1), _option("WORLDBANK", "FP.CPI.TOTL", 2)]
    questions = ["请选择您想要的指标：", "1. a", "2. b", ZH_REPLY_HINT]
    with patch.object(service, "_detect_explicit_provider", return_value="IMF"):
        response = _zh_menu(service, intent, options, questions)

    lines = response.clarificationQuestions
    # Exactly one reply hint, still on the zh prefix the web UI strips.
    hints = [line for line in lines if reply_hint_language(line)]
    assert len(hints) == 1 and reply_hint_language(hints[0]) == "zh"
    assert 'conversationId="conv-1"' in hints[0]
    assert "重新发送选项编号" in hints[0]
    assert "Reply with" not in "\n".join(lines) and "If your client" not in "\n".join(lines)
    # The alternatives disclosure is localized too.
    assert "选项 2 来自 IMF 以外的数据源。" in lines


def test_intentless_zh_reprompt_follows_the_stored_hint_language(service):
    options = [_option("IMF", "PCPI_IX", 1), _option("IMF", "PCPIPCH", 2)]
    response = _zh_menu(service, None, options, ["请选择您想要的指标：", "1. a", "2. b", ZH_REPLY_HINT])
    hint = response.clarificationQuestions[-1]
    assert hint.startswith(CLARIFY_REPLY_HINT_PREFIXES["zh"])
    assert "重新发送选项编号" in hint and "If your client" not in hint


def test_every_reply_hint_catalog_entry_keeps_the_fixed_prefix():
    hint_ids = [mid for mid in _MESSAGES if mid.endswith("reply_hint")]
    assert "clarify_stateless_reply_hint" in hint_ids
    for mid in hint_ids:
        for language, prefix in CLARIFY_REPLY_HINT_PREFIXES.items():
            assert _MESSAGES[mid][language].startswith(prefix), (mid, language)
            assert reply_hint_language(_MESSAGES[mid][language]) == language


@pytest.fixture
def pipeline_service():
    from backend.services.cache import cache_service

    cache_service.clear()
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    svc.settings.use_outcome_decision_stage = False
    svc.settings.use_post_fetch_semantic_judge = False
    svc.settings.use_staged_state_commit = False
    return svc


@pytest.mark.parametrize(
    "provider,code,params,source",
    [
        ("FRED", "M2SL", {"country": "US"}, "FRED"),
        ("WORLDBANK", "NY.GDP.MKTP.CD", {"country": "Germany"}, "World Bank"),
        ("BIS", "BIS_WS_TC", {"country": "Japan"}, "BIS"),
        ("IMF", "NGDP_RPCH", {"country": "United States"}, "IMF"),
    ],
)
def test_reissued_option_query_is_served_through_process_query_without_a_menu(
    pipeline_service, provider, code, params, source,
):
    """A stateless re-send (no conversationId) serves exactly that series, never re-asks."""
    query = clarification.stateless_option_query(pipeline_service, _option(provider, code), _intent(provider, params))
    assert query
    served = NormalizedData(
        metadata=Metadata(source=source, indicator=code, country=params["country"], frequency="annual",
                          unit="x", seriesId=code),
        data=[{"date": "2020-01-01", "value": 1.0}, {"date": "2021-01-01", "value": 2.0}],
    )
    fetch = AsyncMock(return_value=[served])
    with patch.object(pipeline_service.pipeline, "parse_and_route",
                      new=AsyncMock(side_effect=AssertionError("the LLM parse must not run"))), \
         patch.object(pipeline_service, "_fetch_data", new=fetch):
        response = run(pipeline_service.process_query(query))
    assert response.clarificationNeeded is False
    assert response.data and response.data[0].metadata.seriesId == code
    fetched_intent = fetch.await_args_list[0].args[0]
    assert fetched_intent.apiProvider == provider
    assert str(fetched_intent.parameters["indicator"]).upper() == code.upper()


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
    assert response.clarificationQuestions[0] == (
        "No FRED option matched this request; the options below are from other providers."
    )


def test_recovery_with_explicit_provider_fetches_only_that_provider_bounded(collect, pipeline_service, monkeypatch):
    """Explicit-first ranking feeds uncertain-match recovery more named-provider
    candidates; recovery must still fetch ONLY that provider, at most max_options."""
    service = pipeline_service
    monkeypatch.setattr(service, "_get_fallback_providers", lambda *args, **kwargs: ["WORLDBANK"])
    monkeypatch.setattr(service.bis_provider, "_lookup_dataflow_info", lambda code: (None, None), raising=False)
    monkeypatch.setattr(service, "_needs_indicator_clarification", lambda *args, **kwargs: True)
    monkeypatch.setattr(clarification, "_selection_authority_holds", lambda *args, **kwargs: False)
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "_fetch_data", fetch)

    query = "Credit to GDP ratio US from BIS"
    intent = _intent("BIS", {"country": "US"}, query=query, indicators=["credit to GDP ratio"])
    served = NormalizedData(
        metadata=Metadata(source="BIS", indicator="Credit", country="US", frequency="quarterly",
                          unit="x", seriesId="BIS_OTHER"),
        data=[{"date": "2020-01-01", "value": 1.0}],
    )
    result = run(clarification.maybe_recover_from_uncertain_match(service, query, intent, [served]))

    assert result is None  # every candidate fetch came back empty
    fetched = [(call.args[0].apiProvider, call.args[0].parameters["indicator"]) for call in fetch.await_args_list]
    assert fetched, "the named provider's candidates must reach recovery"
    assert {provider for provider, _ in fetched} == {"BIS"}
    assert len({code for _, code in fetched}) <= 4


def test_mixed_menu_labels_other_provider_options_as_alternatives(service):
    intent = _intent("BIS", {"country": "US"}, query="Credit to GDP ratio US from BIS")
    options = [_option("BIS", "BIS_WS_TC", 1), _option("WORLDBANK", "FS.AST.PRVT.GD.ZS", 2)]
    response = clarification.finalize_clarification_response_contract(
        service, _menu_response(options, ["Choose:", "1. a", "2. b"], intent),
    )
    assert response.clarificationQuestions[1] == "Options 2 are from providers other than BIS."
    # Option text/value keep the parseable "[Provider] ... (CODE)" shape.
    assert [clarification.parse_indicator_option(o.value) for o in response.clarificationOptions] == [
        ("BIS", "BIS_WS_TC"), ("WORLDBANK", "FS.AST.PRVT.GD.ZS"),
    ]


NO_DATA_OPTIONS = [
    "[BIS] Total credit (BIS_WS_TC)",
    "[BIS] Credit-to-GDP gap (BIS_WS_CREDIT_GAP)",
    "[WorldBank] Domestic credit (FS.AST.PRVT.GD.ZS)",
]


def _no_data_menu(**kwargs):
    qs = SimpleNamespace(_collect_indicator_choice_options=lambda **kw: list(NO_DATA_OPTIONS))
    intent = _intent("BIS", {"country": "US", "indicator": "BIS_WS_TC"}, query="credit from BIS")
    with patch.object(clarification, "dedupe_indicator_choice_options", side_effect=lambda qs, opts: list(opts)), \
         patch.object(clarification, "store_pending_indicator_options"), \
         patch.object(clarification, "build_clarification_options", return_value=None):
        return clarification.build_no_data_indicator_clarification(qs, "c", "credit from BIS", intent, **kwargs)


@pytest.mark.parametrize(
    "attempt_error",
    [None, DataNotAvailableError("Data not available: 404"), DataNotAvailableError("Invalid request: 400")],
)
def test_no_data_menu_excludes_the_code_that_definitively_returned_nothing(attempt_error):
    response = _no_data_menu(attempt_error=attempt_error)
    assert response is not None
    assert not any("BIS_WS_TC)" in line for line in response.clarificationQuestions)
    assert any("BIS_WS_CREDIT_GAP" in line for line in response.clarificationQuestions)


@pytest.mark.parametrize(
    "attempt_error",
    [ProviderCoolingDown("Rate limit exceeded (429)."), TimeoutError("read timeout"), RuntimeError("HTTP 503")],
)
def test_no_data_menu_keeps_the_code_after_a_transient_failure(attempt_error):
    # A cooldown or transport error says nothing about the series itself.
    response = _no_data_menu(attempt_error=attempt_error)
    assert response is not None
    assert any("BIS_WS_TC)" in line for line in response.clarificationQuestions)


def _provider_layer_error(side_effect, *, open_breaker=False):
    """The exception production code actually catches at query.py's
    ``except DataNotAvailableError`` after a real BaseProvider request."""
    from types import SimpleNamespace as _NS

    import httpx
    from tenacity import wait_none

    from backend.providers import base as base_module
    from backend.providers.base import BaseProvider, _get_breaker
    from backend.services import rate_limiter as rates

    class _P(BaseProvider):
        @property
        def provider_name(self):
            return "RC_OUTAGE_TEST"

        async def _fetch_data(self, **params):
            return []

    url = "https://example.invalid/bis"
    responses = side_effect(httpx, url) if callable(side_effect) else side_effect
    with patch.object(rates, "_global_rate_limiter", rates.GlobalRateLimiter()), \
         patch.object(base_module, "_provider_breakers", {}), \
         patch.object(base_module, "wait_exponential", lambda **kwargs: wait_none()):
        if open_breaker:
            _get_breaker("RC_OUTAGE_TEST").open()
        client = _NS(get=AsyncMock(side_effect=responses))
        try:
            run(_P()._get_with_retry(client, url))
        except DataNotAvailableError as exc:
            return exc
    raise AssertionError("provider request unexpectedly succeeded")


@pytest.mark.parametrize(
    "side_effect,open_breaker",
    [
        (lambda httpx, url: [httpx.Response(503, request=httpx.Request("GET", url))] * 3, False),
        (lambda httpx, url: httpx.ConnectError("connection refused"), False),
        (lambda httpx, url: httpx.ReadTimeout("timed out"), False),
        (lambda httpx, url: [httpx.Response(200, request=httpx.Request("GET", url))], True),
    ],
    ids=["5xx_retries_exhausted", "connect_error", "timeout", "breaker_open"],
)
def test_no_data_menu_keeps_the_code_after_a_real_provider_outage(side_effect, open_breaker):
    # Production outages arrive as the provider layer's typed
    # ProviderUnavailable (a DataNotAvailableError), never TimeoutError.
    error = _provider_layer_error(side_effect, open_breaker=open_breaker)
    assert isinstance(error, DataNotAvailableError)
    response = _no_data_menu(attempt_error=error)
    assert response is not None
    assert any("BIS_WS_TC)" in line for line in response.clarificationQuestions)


def test_no_data_menu_keeps_the_code_after_a_bis_transport_reraise():
    from backend.utils.retry import ProviderUnavailable

    error = ProviderUnavailable(
        "BIS unavailable (transport failure, not a no-data verdict) for WS_TC in US: timed out"
    )
    response = _no_data_menu(attempt_error=error)
    assert any("BIS_WS_TC)" in line for line in response.clarificationQuestions)


def test_no_data_menu_still_excludes_a_real_provider_404():
    error = _provider_layer_error(
        lambda httpx, url: [httpx.Response(404, request=httpx.Request("GET", url))]
    )
    response = _no_data_menu(attempt_error=error)
    assert not any("BIS_WS_TC)" in line for line in response.clarificationQuestions)


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


def test_partial_disclosure_through_the_real_serve_path(pipeline_service):
    """Real _process_query_impl (parse stubbed): rerank/recovery/coverage run as in production."""
    from backend.services.query_pipeline import ParseRouteResult, ValidationResult

    service = pipeline_service
    intent = ParsedIntent(
        apiProvider="FRED", indicators=["unemployment rate", "CPI"],
        parameters={"country": "US", "startDate": "2010-01-01", "__time_scope_authority": "user"},
        clarificationNeeded=False, originalQuery="US unemployment and CPI together since 2010",
    )
    validation = ValidationResult(
        is_multi_indicator=True, is_valid=True, validation_error=None, suggestions=None,
        is_confident=True, confidence_reason=None,
    )

    async def _fetch(_svc, single_intent):
        name = single_intent.indicators[0]
        if "cpi" in name.lower():
            raise DataNotAvailableError("Invalid request: series does not exist")
        return [NormalizedData(
            metadata=Metadata(source="FRED", indicator="Unemployment Rate", country="US",
                              frequency="monthly", unit="Percent", seriesId="UNRATE"),
            data=[{"date": "2020-01-01", "value": 3.5}, {"date": "2021-01-01", "value": 6.4}],
        )]

    with patch.object(service.pipeline, "parse_and_route", new=AsyncMock(return_value=ParseRouteResult(
            intent=intent, explicit_provider=None, routed_provider="FRED", validation_warning=None))), \
         patch.object(service.pipeline, "validate_intent", return_value=validation), \
         patch.object(service, "_build_post_parse_clarification", new=AsyncMock(return_value=None)), \
         patch.object(DF, "fetch_data", new=_fetch):
        response = run(service.process_query(intent.originalQuery))

    assert response.clarificationNeeded is False
    assert [s.metadata.seriesId for s in response.data] == ["UNRATE"]
    assert [(u.indicator, u.reason) for u in response.unservedIndicators] == [("CPI", "no_data")]
    assert response.message and "Could not fetch data for: CPI" in response.message


def test_single_indicator_serve_is_never_announced_as_partial(service):
    intent = _intent("FRED", {"country": "US"}, indicators=["a"])
    response = QueryResponse(conversationId="c", intent=intent, data=[_series("a")], clarificationNeeded=False)
    token = DF.MULTI_INDICATOR_FETCH_REPORTS.set([
        {"unserved": [{"indicator": "b", "reason": "no_data"}],
         "served": {DF.served_series_identity(response.data[0])}},
    ])
    try:
        service._finalize_response_contract(response)  # pylint: disable=protected-access
    finally:
        DF.MULTI_INDICATOR_FETCH_REPORTS.reset(token)
    assert response.message is None and response.unservedIndicators is None


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
