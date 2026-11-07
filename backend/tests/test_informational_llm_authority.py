"""Dispatch-authority tests: the LLM's parsed queryType decides informational
vs data, never a keyword rule.

Mechanism under test (query.py ``_process_query_impl``): a first turn (no
conversation state) must reach the main LLM parse, and the parse's
``queryType`` is the only post-parse authority for the informational handler.
These tests stub the parse (``pipeline.parse_and_route``) and assert which
PATH ran — the informational text handler vs the data pipeline — never a
pinned series id.

History: a question-word x metadata-word set intersection
(``looks_informational``) used to (a) answer first turns before any LLM call
and (b) override the LLM's parsed queryType on every turn, sending
fetch-shaped questions ("list GDP data of G7 countries 2020-2024") to a text
menu with no data. It was English-only, so Chinese and English users were
routed by different rules.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ParsedIntent, QueryResponse
from backend.services import query as query_module
from backend.services.cache import cache_service
from backend.services.conversation import conversation_manager
from backend.services.conversation_state_v2 import ConversationState, FollowUpDelta
from backend.services.delta_extractor import DeltaExtractor
from backend.services.query import QueryService
from backend.services.query_pipeline import ParseRouteResult, ValidationResult
from backend.tests.utils import run


_FETCH_SENTINEL = "__FETCH_PATH_REACHED__"
_INFO_SENTINEL = "__INFORMATIONAL_HANDLER_REACHED__"


@pytest.fixture
def service() -> QueryService:
    cache_service.clear()
    # The parse-result cache is a process-global dict keyed by query text;
    # clear it so a prior test's stubbed parse can never satisfy this one.
    query_module._intent_cache.clear()
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    svc.settings.use_outcome_decision_stage = False
    svc.settings.use_post_fetch_semantic_judge = False
    svc.settings.use_staged_state_commit = False
    yield svc
    query_module._intent_cache.clear()


def _intent(query: str, **overrides: Any) -> ParsedIntent:
    fields: Dict[str, Any] = {
        "apiProvider": "FRED",
        "indicators": ["GDP"],
        "parameters": {"country": "US"},
        "clarificationNeeded": False,
        "confidence": 0.95,
        "originalQuery": query,
    }
    fields.update(overrides)
    return ParsedIntent(**fields)


def _parse_result(intent: ParsedIntent) -> ParseRouteResult:
    return ParseRouteResult(
        intent=intent,
        explicit_provider=None,
        routed_provider=intent.apiProvider,
        validation_warning=None,
    )


def _valid() -> ValidationResult:
    return ValidationResult(
        is_multi_indicator=False,
        is_valid=True,
        validation_error=None,
        suggestions=None,
        is_confident=True,
        confidence_reason=None,
    )


class _Run:
    """Outcome of one stubbed ``process_query`` call."""

    def __init__(self, response: QueryResponse, parse: AsyncMock, info: Any, fetch: AsyncMock,
                 pro: AsyncMock) -> None:
        self.response = response
        self.parse = parse
        self.info = info
        self.fetch = fetch
        self.pro = pro

    @property
    def reached_fetch(self) -> bool:
        return self.fetch.await_count > 0

    @property
    def reached_informational(self) -> bool:
        return self.info.call_count > 0


def _process(
    service: QueryService,
    query: str,
    parsed: ParsedIntent,
    *,
    conversation_id: Optional[str] = None,
    auto_pro_mode: bool = False,
    real_informational_handler: bool = False,
) -> _Run:
    """Drive ``process_query`` with the parse stubbed.

    The data path is observed at ``_build_post_parse_clarification`` — the
    first data-pipeline stage after validation — which returns a sentinel so
    no provider is contacted. The informational handler is wrapped (or
    stubbed) so its invocation is observable.
    """
    parse = AsyncMock(return_value=_parse_result(parsed))
    fetch = AsyncMock(return_value=QueryResponse(
        conversationId="c", clarificationNeeded=False, message=_FETCH_SENTINEL,
    ))
    pro = AsyncMock(return_value=QueryResponse(
        conversationId="c", clarificationNeeded=False, message="__PRO_MODE__",
    ))
    real_handler = service._handle_informational_intent

    calls: List[Dict[str, Any]] = []

    def _info(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if real_informational_handler:
            return real_handler(**kwargs)
        return QueryResponse(
            conversationId=kwargs["conversation_id"],
            clarificationNeeded=False,
            message=_INFO_SENTINEL,
        )

    class _Recorder:
        @property
        def call_count(self) -> int:
            return len(calls)

        @property
        def calls(self) -> List[Dict[str, Any]]:
            return calls

    with patch.object(service.pipeline, "parse_and_route", parse), \
         patch.object(service.pipeline, "validate_intent", return_value=_valid()), \
         patch.object(service, "_build_exact_indicator_title_intent", return_value=None), \
         patch.object(service, "_handle_informational_intent", side_effect=_info), \
         patch.object(service, "_build_post_parse_clarification", fetch), \
         patch.object(service, "_execute_pro_mode", pro):
        response = run(service.process_query(
            query,
            conversation_id=conversation_id,
            auto_pro_mode=auto_pro_mode,
            use_orchestrator=False,
            allow_orchestrator=False,
        ))
    return _Run(response, parse, _Recorder(), fetch, pro)


# ── (a) fetch-shaped questions the LLM labels data → data pipeline ─────────

# Each phrasing carries a question word ("list"/"does"/"which"/"what") AND a
# metadata word ("data"/"series") — exactly the shape the deleted keyword
# rule captured — yet asks for actual values.
_FETCH_SHAPED = [
    ("list GDP data of G7 countries 2020-2024", "data_fetch"),
    ("does japan have higher unemployment data than korea", "comparison"),
    ("which countries have the highest inflation data", "comparison"),
    ("what does the latest china cpi data show", "data_fetch"),
    ("show me the series for US housing starts", "data_fetch"),
    # Shape of a stateless API/agent ask that was answered with an indicator
    # menu in 0.07s: "list ... series ... values" for a named provider.
    (
        "China real residential property prices index BIS quarterly series "
        "from 2016 to 2024, list all quarterly values",
        "data_fetch",
    ),
]


@pytest.mark.parametrize("query,query_type", _FETCH_SHAPED)
def test_first_turn_fetch_shaped_question_follows_llm_data_label(
    service: QueryService, query: str, query_type: str,
) -> None:
    result = _process(service, query, _intent(query, queryType=query_type))

    assert result.parse.await_count == 1, "first turn must reach the main LLM parse"
    assert not result.reached_informational
    assert result.reached_fetch
    assert result.response.message == _FETCH_SENTINEL


def test_follow_up_llm_data_label_is_not_overridden(service: QueryService) -> None:
    """With conversation state, the delta classifier says new_query and the
    parse says comparison — no rule may flip that to informational."""
    conv_id = conversation_manager.get_or_create("conv-info-authority-follow-up")
    prior = _intent("US unemployment rate", indicators=["unemployment rate"])
    conversation_manager.add_message_safe(conv_id, "user", "US unemployment rate", intent=prior)
    conversation_manager.set_conversation_state(
        conv_id, ConversationState(indicator="unemployment rate", country="US", provider="FRED"),
    )
    query = "does japan have higher unemployment data than korea"
    parsed = _intent(
        query,
        queryType="comparison",
        indicators=["unemployment rate"],
        parameters={"countries": ["JP", "KR"]},
    )

    with patch.object(DeltaExtractor, "extract", return_value=None), \
         patch.object(DeltaExtractor, "extract_with_llm", new_callable=AsyncMock,
                      return_value=FollowUpDelta(query_type="new_query")):
        result = _process(service, query, parsed, conversation_id=conv_id)

    assert not result.reached_informational
    assert result.reached_fetch


# ── (b) questions the LLM labels informational → informational handler ────

_INFORMATIONAL = [
    ("what indicators does FRED have for housing", ["housing"], "FRED"),
    ("what data sources do you have", ["data sources"], "WorldBank"),
    # Chinese: the deleted rule was English-only and returned False here; the
    # LLM label is language-independent.
    ("有哪些指标", ["indicators"], "WorldBank"),
    ("世界银行有哪些GDP指标", ["GDP"], "WorldBank"),
]


@pytest.mark.parametrize("query,indicators,provider", _INFORMATIONAL)
def test_first_turn_llm_informational_label_reaches_handler(
    service: QueryService, query: str, indicators: List[str], provider: str,
) -> None:
    parsed = _intent(query, queryType="informational", indicators=indicators, apiProvider=provider)

    result = _process(service, query, parsed)

    # The parse ran (no pre-LLM shortcut answered first) and its label won.
    assert result.parse.await_count == 1
    assert result.reached_informational
    assert not result.reached_fetch
    assert result.response.message == _INFO_SENTINEL
    # The handler receives the PARSED intent (real search terms/provider),
    # not a placeholder built before any LLM call.
    handed = result.info.calls[0]["intent"]
    assert handed.indicators == indicators


def test_catalog_question_not_captured_by_pre_parse_gates_with_auto_pro_mode(
    service: QueryService,
) -> None:
    """Without the old first-turn shortcut, catalog questions now pass the
    pre-parse gates (geography-breakdown clarifier, auto Pro Mode complexity
    analyzer, orchestrator). They must still reach the parse and the handler."""
    query = "what indicators does FRED have for housing"
    parsed = _intent(query, queryType="informational", indicators=["housing"])

    result = _process(service, query, parsed, auto_pro_mode=True)

    assert result.pro.await_count == 0
    assert result.parse.await_count == 1
    assert result.reached_informational


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Documented debt: the early auto-Pro-Mode gate (QueryComplexityAnalyzer, "
        "keyword patterns such as 'each province') runs BEFORE the parse on first "
        "turns, so it can capture a catalog question the LLM would label "
        "informational. General fix: evaluate the early complexity gate after the "
        "parse and skip it when the parsed queryType is 'informational' (consumes "
        "the LLM label, no keyword carve-out). Only applies to registered users "
        "(auto_pro_mode)."
    ),
)
def test_catalog_question_with_decomposition_words_beats_auto_pro_mode(
    service: QueryService,
) -> None:
    query = "what indicators are available for each province"
    parsed = _intent(query, queryType="informational", indicators=["province indicators"])

    result = _process(service, query, parsed, auto_pro_mode=True)

    assert result.pro.await_count == 0
    assert result.reached_informational


# ── (c) a missing / off-contract label takes the data path ────────────────

@pytest.mark.parametrize("raw_query_type", ["__absent__", None, "", "ranking", "  Data_Fetch  "])
def test_missing_or_invalid_query_type_takes_data_path(
    service: QueryService, raw_query_type: Any,
) -> None:
    # Built the way the parser builds it (ParsedIntent(**parsed) from LLM JSON),
    # for a phrasing the deleted keyword rule would have sent to the text menu.
    query = "which data series are available for US housing starts since 2020"
    payload: Dict[str, Any] = {
        "apiProvider": "FRED",
        "indicators": ["housing starts"],
        "parameters": {"country": "US", "startDate": "2020-01-01"},
        "clarificationNeeded": False,
        "confidence": 0.9,
        "originalQuery": query,
    }
    if raw_query_type != "__absent__":
        payload["queryType"] = raw_query_type
    parsed = ParsedIntent(**payload)
    assert parsed.queryType == "data_fetch"

    result = _process(service, query, parsed)

    assert not result.reached_informational
    assert result.reached_fetch


def test_query_type_normalizer_keeps_contract_values() -> None:
    base = {"apiProvider": "FRED", "indicators": ["GDP"], "clarificationNeeded": False}
    for value, expected in [
        ("informational", "informational"),
        (" Informational ", "informational"),
        ("COMPARISON", "comparison"),
        ("analysis", "analysis"),
        ("data_fetch", "data_fetch"),
        ("follow_up", "data_fetch"),
        (None, "data_fetch"),
    ]:
        assert ParsedIntent(**base, queryType=value).queryType == expected
