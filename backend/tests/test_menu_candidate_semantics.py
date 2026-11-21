"""Successful transport cannot authorize core/index/rate menu substitutions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services import indicator_clarification as clarify
from backend.services.indicator_selector import SelectionResult
from backend.services.query import QueryService
from backend.services.semantic_match_judge import SemanticMatchJudgment


def _series(name="Headline CPI annual change", country="China", code="FP.CPI.TOTL.ZG"):
    return NormalizedData(metadata=Metadata(source="World Bank", indicator=name, country=country,
                          frequency="annual", unit="percent", seriesId=code),
                          data=[{"date": "2024-01-01", "value": 2.0}])


def _intent(query="中国CPI近5年"):
    return ParsedIntent(apiProvider="WORLDBANK", indicators=["CPI"], parameters={"country": "China"},
                        originalQuery=query, language="zh", clarificationNeeded=False)


def _service(data):
    return SimpleNamespace(_fetch_data=AsyncMock(return_value=data),
                           _rerank_data_by_query_relevance=lambda query, result: result,
                           _is_ranking_query=lambda query: False,
                           _served_data_references_region=lambda data, region: True)


@pytest.mark.parametrize("decision,confidence,reason", [
    ("match", 0.95, "viable"),
    ("mismatch", 0.95, "substantive:semantic_mismatch"),
    ("uncertain", 0.7, "transient:semantic_uncertain"),
    ("match", 0.5, "transient:semantic_uncertain"),
    ("mismatch", 0.5, "transient:semantic_uncertain"),
])
async def test_fetched_candidate_requires_its_own_affirmative_judgment(monkeypatch, decision, confidence, reason):
    candidate = _series("Core CPI, seasonally adjusted", code="CORESA")
    judge = AsyncMock(return_value=SemanticMatchJudgment(decision=decision, confidence=confidence))
    monkeypatch.setattr(clarify, "judge_fallback_result", judge)
    intent = _intent()
    intent.parameters.update(__semantic_authority="llm_adjudication", __authoritative_pick_code="OTHER")
    data, result = await clarify._validate_indicator_choice_option(
        _service([candidate]), intent.originalQuery, intent, "[WorldBank] Core CPI (CORESA)")
    assert result == reason
    assert bool(data) is (reason == "viable")
    assert judge.await_args.kwargs["original_indicators"] == ["CPI"]
    assert judge.await_args.kwargs["fallback_result"][0].metadata.indicator.startswith("Core CPI")


@pytest.mark.parametrize("failure", [None, RuntimeError("model offline")])
async def test_unavailable_judge_is_not_a_definitive_candidate_rejection(monkeypatch, failure):
    judge = AsyncMock(side_effect=failure) if failure else AsyncMock(return_value=None)
    monkeypatch.setattr(clarify, "judge_fallback_result", judge)
    intent = _intent()
    data, reason = await clarify._validate_indicator_choice_option(
        _service([_series()]), intent.originalQuery, intent, "[WorldBank] Headline CPI (FP.CPI.TOTL.ZG)")
    assert data is None and reason == "transient:judge_unavailable"


async def test_a_country_mismatch_cannot_borrow_semantic_acceptance(monkeypatch):
    judge = AsyncMock(side_effect=AssertionError("structurally wrong country must stop before model"))
    monkeypatch.setattr(clarify, "judge_fallback_result", judge)
    data, reason = await clarify._validate_indicator_choice_option(
        _service([_series(country="Peru")]), "China CPI", _intent(), "[WorldBank] CPI (FP.CPI.TOTL.ZG)")
    assert data is None and reason == "substantive:country_not_covered"
    judge.assert_not_awaited()


async def test_unknown_returned_country_is_unverified_not_a_definitive_rejection(monkeypatch):
    candidate = _series()
    candidate.metadata.country = None
    judge = AsyncMock(side_effect=AssertionError("unknown scope cannot authorize the candidate"))
    monkeypatch.setattr(clarify, "judge_fallback_result", judge)
    data, reason = await clarify._validate_indicator_choice_option(
        _service([candidate]), "China CPI", _intent(), "[WorldBank] CPI (FP.CPI.TOTL.ZG)")
    assert data is None and reason == "transient:scope_unverified"


@pytest.mark.parametrize("country,reason", [("Georgia", "transient:scope_unverified"),
                                           ("GE", "substantive:country_not_covered")])
async def test_colliding_bare_country_metadata_is_unknown_but_explicit_foreign_iso_is_not(monkeypatch, country, reason):
    candidate = _series("Gross Domestic Product in Georgia", country=country, code="GANGSP")
    candidate.metadata.source = "FRED"
    intent = _intent("GDP in Georgia, United States")
    intent.parameters["country"] = "US"
    data, result = await clarify._validate_indicator_choice_option(
        _service([candidate]), intent.originalQuery, intent, "[FRED] GDP in Georgia (GANGSP)")
    assert data is None and result == reason


@pytest.mark.parametrize("provider,code", [("WORLDBANK", "CPTOTSAXN"), ("IMF", "PCPIPCH")])
async def test_native_user_choice_preserves_short_code_and_full_request_scope(monkeypatch, provider, code):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    selector = AsyncMock(side_effect=AssertionError("a native user choice must not be selected again"))
    monkeypatch.setattr("backend.services.indicator_selector.IndicatorSelector.select", selector)
    params = {"indicator": code, "country": "CN", "startDate": "2021-01-01",
              "endDate": "2025-12-31", "__time_scope_authority": "user",
              "__semantic_authority": "exact_user_input", "__decision_source": "user_choice"}
    intent = ParsedIntent(apiProvider=provider, indicators=[code], parameters=params.copy(),
                          originalQuery="China CPI", clarificationNeeded=False)
    result = await service._resolve_indicator_for_fetch(provider, intent, params.copy())
    assert result["indicator"] == code
    assert {key: result[key] for key in ("country", "startDate", "endDate", "__time_scope_authority")} == {
        key: params[key] for key in ("country", "startDate", "endDate", "__time_scope_authority")}
    assert not result.get("__exact_provider_code_match")
    assert not result.get("__exact_indicator_title_match")
    selector.assert_not_awaited()


async def test_sole_verified_option_mints_current_native_pair_without_history_override(monkeypatch):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    intent = _intent("China CPI")
    intent.parameters.update(startDate="2021-01-01", endDate="2025-12-31", __time_scope_authority="default")
    data = [_series("CPI price", code="CPTOTSAXN")]
    option = clarify._executed_indicator_option(service, "[WorldBank] CPI (CPTOTSAXN)", data)
    clarify._option_validation_snapshots(intent)[option] = clarify._option_result_snapshot(data)
    assert clarify.apply_indicator_option_to_intent(intent, option)
    assert intent.parameters["__authoritative_pick_code"] == "CPTOTSAXN"
    assert intent.parameters["__authoritative_pick_provider"] == "WORLDBANK"
    assert intent.parameters["__semantic_indicator_label"] == "CPI"
    selector = AsyncMock(side_effect=AssertionError("fresh verified identity must stay selected"))
    monkeypatch.setattr("backend.services.indicator_selector.IndicatorSelector.select", selector)
    result = await service._resolve_indicator_for_fetch("WORLDBANK", intent, intent.parameters.copy())
    assert result["indicator"] == "CPTOTSAXN" and result["startDate"] == "2021-01-01"
    assert not result.get("__exact_provider_code_match")


@pytest.mark.parametrize("changed", ["provider", "code", "excluded"])
async def test_stale_or_excluded_native_pair_does_not_authorize_the_short_code(monkeypatch, changed):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    params = {"indicator": "CPTOTSAXN", "country": "CN", "__semantic_authority": "llm_adjudication",
              "__authoritative_pick_code": "CPTOTSAXN", "__authoritative_pick_provider": "WORLDBANK"}
    if changed == "provider":
        params["__authoritative_pick_provider"] = "FRED"
    elif changed == "code":
        params["__authoritative_pick_code"] = "OTHER"
    else:
        params["__exclude_indicator_codes"] = ["CPTOTSAXN"]
    intent = ParsedIntent(apiProvider="WORLDBANK", indicators=["CPI"], parameters=params,
                          originalQuery="China CPI", clarificationNeeded=False)
    selector = AsyncMock(return_value=SelectionResult(code="FP.CPI.TOTL", name="Consumer price index", source="llm_pick"))
    monkeypatch.setattr("backend.services.indicator_selector.IndicatorSelector.select", selector)
    result = await service._resolve_indicator_for_fetch("WORLDBANK", intent, params.copy())
    assert selector.await_count
    assert result["indicator"] == "FP.CPI.TOTL"


@pytest.mark.parametrize("decision,identity", [("match", "same"), ("uncertain", "same"),
                                              ("mismatch", "same"), ("match", "different"),
                                              ("match", "missing")])
async def test_legacy_menu_click_checks_native_identity_then_requires_selected_label_judgment(monkeypatch, decision, identity):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    intent = _intent("China CPI")
    option = "[WorldBank] Consumer price index (FP.CPI.TOTL)"
    pending = {"original_query": intent.originalQuery, "intent": intent.model_dump(), "options": [option]}
    monkeypatch.setattr(clarify.conversation_manager, "get_pending_indicator_options", lambda cid: pending)
    monkeypatch.setattr(clarify.conversation_manager, "clear_pending_indicator_options", Mock())
    monkeypatch.setattr(clarify.conversation_manager, "set_pending_indicator_options", Mock())
    data = _series("Consumer price index", code="FP.CPI.TOTL")
    if identity == "different":
        data.metadata.seriesId = "OTHER"
    elif identity == "missing":
        data.metadata.seriesId = None
    monkeypatch.setattr(service, "_fetch_data", AsyncMock(return_value=[data]))
    monkeypatch.setattr(clarify, "maybe_recover_from_uncertain_match", AsyncMock(side_effect=AssertionError("native click cannot be replaced")))
    judge = AsyncMock(return_value=SemanticMatchJudgment(decision=decision, confidence=0.95))
    monkeypatch.setattr(clarify, "judge_fallback_result", judge)
    response = await clarify.try_resolve_pending_indicator_choice(service, "1", "legacy-native-click")
    assert bool(response.data) is (identity == "same" and decision == "match")
    if identity == "same":
        judge.assert_awaited_once()
        assert judge.await_args.kwargs["original_indicators"] == ["Consumer price index"]
        assert '"country": "China"' in judge.await_args.kwargs["original_query"]
    else:
        judge.assert_not_awaited()
    if identity == "missing" or decision == "uncertain":
        assert response.error == "verification_unavailable"


async def test_a_single_candidate_is_validated_and_records_nonserialized_evidence(monkeypatch):
    judge = AsyncMock(return_value=SemanticMatchJudgment(decision="mismatch", confidence=0.95))
    monkeypatch.setattr(clarify, "judge_fallback_result", judge)
    intent = _intent()
    option = "[WorldBank] Core CPI (CORESA)"
    service = _service([_series("Core CPI", code="CORESA")])
    evidence = {}
    result = await clarify.filter_viable_indicator_choice_options(
        service, intent.originalQuery, intent, [option], collector=evidence)
    assert result == []
    assert evidence["dropped"][option] == "substantive:semantic_mismatch"
    assert clarify._without_rejected_indicator_options(intent, [option]) == []
    assert "_indicator_option_validation" not in intent.model_dump()
    assert clarify._without_rejected_indicator_options(intent.model_copy(deep=True), [option]) == []
    intent.originalQuery = "China core CPI"
    assert clarify._without_rejected_indicator_options(intent, [option]) == [option]


@pytest.mark.parametrize("builder", ["no_data", "uncertain"])
def test_later_menu_paths_do_not_reintroduce_definitively_rejected_candidates(monkeypatch, builder):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    intent = _intent("China CPI")
    rejected = "[WorldBank] Core CPI (CORESA)"
    alternatives = ["[FRED] Headline CPI index (CHNCPIALLMINMEI)"]
    clarify._option_validation_evidence(intent)[("WORLDBANK", "CORESA")] = "substantive:semantic_mismatch"
    monkeypatch.setattr(service, "_collect_indicator_choice_options", Mock(return_value=[rejected, *alternatives]))
    monkeypatch.setattr(service, "_needs_indicator_clarification", Mock(return_value=True))
    if builder == "no_data":
        response = clarify.build_no_data_indicator_clarification(service, "rejected-menu", "China CPI", intent)
    else:
        response = clarify.build_uncertain_result_clarification(
            service, "rejected-menu", "China CPI", intent,
            [_series("Core CPI", code="CORESA")])
    assert response is None or not response.clarificationOptions


async def test_catalog_aliases_collapse_to_actual_executed_option_and_cannot_resurface(monkeypatch):
    monkeypatch.setattr(clarify, "judge_fallback_result", AsyncMock(return_value=
                        SemanticMatchJudgment(decision="match", confidence=0.95)))
    intent = _intent()
    data = [_series("Consumer price index (2010 = 100)", code="FP.CPI.TOTL")]
    data[0].metadata.unit = "2010 = 100"
    service = _service(data)
    aliases = ["[WorldBank] CPI, seasonally adjusted (CPTOTSAXN)",
               "[WorldBank] CPI, not adjusted (CPTOTNSXN)"]
    evidence = {}
    options = await clarify.filter_viable_indicator_choice_options(
        service, intent.originalQuery, intent, aliases, collector=evidence)
    assert len(options) == 1
    assert clarify.parse_indicator_option(options[0]) == ("WORLDBANK", "FP.CPI.TOTL")
    assert "annual" in options[0] and "2010 = 100" in options[0]
    assert "seasonally adjusted" not in options[0]
    assert evidence["validated"][options[0]] == data
    assert clarify._without_rejected_indicator_options(intent, aliases) == options
    clarify._option_validation_evidence(intent)[("WORLDBANK", "FP.CPI.TOTL")] = "substantive:semantic_mismatch"
    assert clarify._without_rejected_indicator_options(intent, aliases) == []


def test_executed_identity_checks_every_country_and_preserves_common_native_code():
    service = SimpleNamespace()
    china = _series(code="FP.CPI.TOTL")
    peru = _series(country="Peru", code="FP.CPI.TOTL")
    option = clarify._executed_indicator_option(service, "[WorldBank] CPI (FP.CPI.TOTL)", [china, peru])
    assert option and clarify.parse_indicator_option(option) == ("WORLDBANK", "FP.CPI.TOTL")
    assert {row["country"] for row in clarify._option_result_snapshot([china, peru])} == {"CN", "PE"}
    peru.metadata.seriesId = "OTHER"
    assert clarify._executed_indicator_option(service, option, [china, peru]) is None
    peru.metadata.seriesId = "FP.CPI.TOTL"
    peru.metadata.unit = "Different basis"
    assert clarify._executed_indicator_option(service, option, [china, peru]) is None


def test_executed_identity_keeps_supported_transform_and_abstains_on_unreplayable_coordinate():
    data = _series("GDP percent change", country="US", code="GDP")
    data.metadata.source = "FRED"
    option = clarify._executed_indicator_option(SimpleNamespace(), "[FRED] GDP growth (GDP:pc1)", [data])
    assert clarify.parse_indicator_option(option) == ("FRED", "GDP:pc1")
    data.metadata.source = "StatsCan"
    data.metadata.seriesId = "36100103:1.1.1.0.0.0.0.0.0.0"
    assert clarify._executed_indicator_option(SimpleNamespace(), "[StatsCan] GDP (36100103)", [data]) is None


@pytest.mark.parametrize("actual_code,matches", [
    ("36100103:1.2", True),
    ("36100103:1.2.0.0.0.0.0.0.0.0", True),
    ("36100104:1.2", False),
    ("36100103:", None),
    ("36100103:0.0", None),
    ("36100103:1.2.member", None),
    ("36100103:1.٢", None),
    ("36100103:1.2.0.0.0.0.0.0.0.0.0", None),
])
def test_legacy_statscan_table_choice_uses_native_coordinate_identity(actual_code, matches):
    data = _series("Canadian GDP", country="Canada", code=actual_code)
    data.metadata.source = "Statistics Canada"
    assert clarify._selected_native_identity_matches("[StatsCan] GDP (36100103)", [data]) is matches


@pytest.mark.parametrize("changed", ["seriesId", "unit", "frequency", "country"])
async def test_verified_menu_click_does_not_serve_a_changed_identity_or_measure(monkeypatch, changed):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    intent = _intent("China CPI")
    original = [_series("Consumer price index", code="FP.CPI.TOTL")]
    option = clarify._executed_indicator_option(service, "[WorldBank] CPI (FP.CPI.TOTL)", original)
    clarify._option_validation_snapshots(intent)[option] = clarify._option_result_snapshot(original)
    saved = []
    monkeypatch.setattr(clarify.conversation_manager, "set_pending_indicator_options",
                        lambda cid, payload: saved.append(payload))
    clarify.store_pending_indicator_options(service, "verified-click", intent.originalQuery, intent, [option])
    monkeypatch.setattr(clarify.conversation_manager, "get_pending_indicator_options", lambda cid: saved[-1])
    monkeypatch.setattr(clarify.conversation_manager, "clear_pending_indicator_options", Mock())
    actual = original[0].model_copy(deep=True)
    setattr(actual.metadata, changed, "PE" if changed == "country" else "changed")
    monkeypatch.setattr(service, "_fetch_data", AsyncMock(return_value=[actual]))
    response = await clarify.try_resolve_pending_indicator_choice(service, "1", "verified-click")
    assert not response.data
    assert "did not preserve" in response.error


async def test_verified_replay_bypasses_semantic_reselection_and_failed_replay_keeps_other_snapshots(monkeypatch):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    intent = _intent("China CPI")
    data = [_series("Consumer price index", code="FP.CPI.TOTL")]
    other = [_series("Annual headline inflation", code="FP.CPI.TOTL.ZG")]
    options = [clarify._executed_indicator_option(service, "[WorldBank] CPI (FP.CPI.TOTL)", data),
               clarify._executed_indicator_option(service, "[WorldBank] Inflation (FP.CPI.TOTL.ZG)", other)]
    for option, rows in zip(options, [data, other]):
        clarify._option_validation_snapshots(intent)[option] = clarify._option_result_snapshot(rows)
    saved = []
    monkeypatch.setattr(clarify.conversation_manager, "set_pending_indicator_options",
                        lambda cid, payload: saved.append(payload))
    clarify.store_pending_indicator_options(service, "verified-replay", intent.originalQuery, intent, options)
    pending = saved[-1]
    monkeypatch.setattr(clarify.conversation_manager, "get_pending_indicator_options", lambda cid: pending)
    monkeypatch.setattr(clarify.conversation_manager, "clear_pending_indicator_options", Mock())
    monkeypatch.setattr(clarify, "maybe_recover_from_uncertain_match", AsyncMock(side_effect=AssertionError("verified replay is final")))
    monkeypatch.setattr(clarify, "build_uncertain_result_clarification", Mock(side_effect=AssertionError("verified replay is final")))

    async def fetch(candidate_intent):
        candidate_intent.parameters["__semantic_authority"] = "llm_adjudication"
        returned = data[0].model_copy(deep=True)
        returned.metadata.indicator = "Consumer Price Index"
        return [returned]

    monkeypatch.setattr(service, "_fetch_data", AsyncMock(side_effect=fetch))
    response = await clarify.try_resolve_pending_indicator_choice(service, "1", "verified-replay")
    assert response.data and response.data[0].metadata.seriesId == "FP.CPI.TOTL"
    wrong = data[0].model_copy(deep=True)
    wrong.metadata.seriesId = "OTHER"
    monkeypatch.setattr(service, "_fetch_data", AsyncMock(return_value=[wrong]))
    response = await clarify.try_resolve_pending_indicator_choice(service, "1", "verified-replay")
    assert not response.data
    assert options[1] in saved[-1]["verified_options"]
    assert options[0] not in saved[-1]["verified_options"]


async def test_same_executed_identity_dedupes_title_variations_but_not_conflicting_metadata(monkeypatch):
    monkeypatch.setattr(clarify, "judge_fallback_result", AsyncMock(return_value=
                        SemanticMatchJudgment(decision="match", confidence=0.95)))
    first = _series("Consumer price index", code="FP.CPI.TOTL")
    second = first.model_copy(deep=True)
    second.metadata.indicator = "CPI headline index"
    service = _service(None)
    service._fetch_data.side_effect = [[first], [second]]
    aliases = ["[WorldBank] CPI adjusted (CPTOTSAXN)", "[WorldBank] CPI unadjusted (CPTOTNSXN)"]
    options = await clarify.filter_viable_indicator_choice_options(service, "China CPI", _intent(), aliases)
    assert len(options) == 1
    second.metadata.unit = "Different basis"
    service._fetch_data.side_effect = [[first], [second]]
    evidence = {}
    options = await clarify.filter_viable_indicator_choice_options(service, "China CPI", _intent(), aliases, collector=evidence)
    assert not options
    assert all(reason == "transient:identity_conflict" for reason in evidence["dropped"].values())


@pytest.mark.parametrize("country,region,expected", [
    ("China", None, "CPI China"),
    ("US", "Georgia", "Georgia CPI"),
    ("US", None, "CPI"),
])
def test_catalog_options_use_existing_geographic_retrieval_context(monkeypatch, country, region, expected):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    monkeypatch.setattr(service, "_select_indicator_query_for_resolution", Mock(return_value="CPI"))
    monkeypatch.setattr(service, "_get_fallback_providers", Mock(return_value=[]))
    retrieve = Mock(return_value=([], []))
    monkeypatch.setattr("backend.services.indicator_selector.IndicatorSelector._get_candidates_with_scores", retrieve)
    intent = ParsedIntent(apiProvider="FRED", indicators=["consumer price index"],
                          parameters={"country": country}, subnationalRegion=region,
                          originalQuery="消费者价格指数", language="zh", clarificationNeeded=False)
    clarify.collect_indicator_choice_options(service, intent.originalQuery, intent)
    fred_call = next(call for call in retrieve.call_args_list if call.args[1] == "FRED")
    assert fred_call.args[0] == expected
    assert fred_call.kwargs["english_query"] == f"consumer price index {country}"
    assert fred_call.kwargs["constraint_query"] == intent.originalQuery


async def test_established_trusted_primary_is_not_rejudged_as_a_menu_candidate(monkeypatch):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    monkeypatch.setattr(service.settings, "use_outcome_decision_stage", False)
    monkeypatch.setattr(service, "_select_indicator_query_for_resolution", Mock(return_value="CPI"))
    judge = AsyncMock(side_effect=AssertionError("trusted direct pick is not a menu candidate"))
    monkeypatch.setattr(clarify, "judge_fallback_result", judge)
    intent = _intent("China inflation")
    intent.parameters.update(indicator="FP.CPI.TOTL.ZG", __semantic_authority="llm_adjudication")
    response = await service._build_prefetch_indicator_choice_clarification(
        "trusted-primary-test", intent.originalQuery, intent, None, False)
    assert response is None
    judge.assert_not_awaited()


@pytest.mark.parametrize("unknown", [True, False])
async def test_rejected_options_are_not_resurrected_as_a_menu(monkeypatch, unknown):
    service = QueryService(openrouter_key="test", fred_key="test", comtrade_key="test")
    monkeypatch.setattr(service.settings, "use_outcome_decision_stage", False)
    monkeypatch.setattr(service, "_select_indicator_query_for_resolution", Mock(return_value="CPI"))
    monkeypatch.setattr(service, "_collect_indicator_choice_options", Mock(return_value=[]))
    monkeypatch.setattr(service, "_get_fallback_providers", Mock(return_value=[]))
    selection = SelectionResult(source="user_choice", options=[
        {"code": "CORESA", "name": "Core CPI"},
        {"code": "FP.CPI.TOTL.ZG", "name": "Headline CPI"},
    ])
    monkeypatch.setattr("backend.services.indicator_selector.IndicatorSelector.select", AsyncMock(return_value=selection))

    async def validation(**kwargs):
        options = kwargs["options"]
        kwargs["collector"].update(validated={}, dropped={
            options[0]: "substantive:semantic_mismatch",
            options[1]: "transient:judge_unavailable" if unknown else "substantive:semantic_mismatch",
        })
        return []

    monkeypatch.setattr(service, "_filter_viable_indicator_choice_options", validation)
    intent = _intent("China CPI")
    response = await service._build_prefetch_indicator_choice_clarification(
        "candidate-menu-test", intent.originalQuery, intent, None, False)
    assert not response.clarificationOptions
    assert not response.data
    if unknown:
        assert response.error == "verification_unavailable"
        assert "暂时" in response.message
