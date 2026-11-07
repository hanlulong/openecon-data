"""Empty geography + one established aggregate entity -> filled, never member-expanded.

Covers the aggregate-geography-scope cluster (2026-11-07):
  A2  apply_country_overrides fills EMPTY/default-US geography with the ONE
      established aggregate entity the query names (structural entity
      extraction, same mechanism as the concrete-country fill), vetoed by the
      existing member/comparison/ranking markers and trade-flow structure.
  A3  a provider-default geography (Eurostat EU27_2020, IMF USA) is disclosed,
      never served silently, and an aggregate entity is never fanned out to
      member series by a provider resolver (IMF, BIS, Eurostat).
  B   a group-scope follow-up ("what about France") executes the ORIGINAL
      request with the region substituted, not the bare follow-up text.
  C1  a clarification with a blank message gets readable text.
Assertions check entity identity (canonical_aggregate_region), never a pinned
provider code.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from backend.models import ClarificationOption, Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.routing.country_resolver import CountryResolver
from backend.services import data_fetcher
from backend.services.conversation import conversation_manager
from backend.services.indicator_clarification import (
    build_group_scope_clarification,
    build_group_scope_follow_up_query,
    has_explicit_group_scope,
    has_explicit_member_group_scope,
)
from backend.services.provider_strategy import (
    PROVIDER_GROUP_AGGREGATES,
    canonical_aggregate_region,
    country_qualified_indicator_text,
    normalize_aggregate_geography,
)
from backend.services.query import QueryService, _get_cached_parse_result, _put_cached_parse_result
from backend.services.query_pipeline import QueryPipeline, ValidationResult
from backend.services.simplified_prompt import SimplifiedPrompt
from backend.utils.retry import DataNotAvailableError


def _svc() -> QueryService:
    return QueryService.__new__(QueryService)


def _intent(query: str, params=None, provider: str = "EUROSTAT", **extra) -> ParsedIntent:
    return ParsedIntent(
        apiProvider=provider,
        indicators=["inflation rate"],
        parameters=dict(params or {}),
        clarificationNeeded=False,
        originalQuery=query,
        **extra,
    )


def _series(country: str, source: str = "Eurostat", indicator: str = "HICP") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country=country,
            frequency="monthly", unit="%", seriesId="X1",
        ),
        data=[{"date": "2025-01-01", "value": 2.0}],
    )


# --------------------------------------------------------------------------
# A2: fill empty geography with exactly one established aggregate entity
# --------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_region", [
    ("euro area inflation rate", "EUROZONE"),
    ("eurozone unemployment rate", "EUROZONE"),
    ("inflation in the euro zone since 2020", "EUROZONE"),
    ("ECB policy rate euro area", "EUROZONE"),
    ("EU GDP growth", "EU"),
    ("European Union unemployment rate", "EU"),
    # Aggregate markers no longer veto: they ask for the group value.
    ("euro area inflation rate as a whole", "EUROZONE"),
    ("overall eurozone GDP growth", "EUROZONE"),
    # An explicitly named provider never blocks the fill.
    ("Eurostat euro area inflation", "EUROZONE"),
])
@pytest.mark.parametrize("params", [{}, {"country": "US"}, {"countries": ["US"]}])
def test_null_or_default_us_parse_fills_the_named_aggregate_entity(query, expected_region, params):
    intent = _intent(query, params)
    _svc()._apply_country_overrides(intent, query)
    assert canonical_aggregate_region(intent.parameters.get("country")) == expected_region
    assert not intent.parameters.get("countries")


@pytest.mark.parametrize("query,params,extra", [
    # A concrete country (US included) is named -> concrete fill rules apply.
    ("US imports from the euro area", {}, {}),
    ("euro area vs US inflation", {}, {}),
    # Member / comparison / ranking scope markers veto (fail-closed).
    ("compare euro area member countries inflation", {}, {}),
    ("EU countries unemployment", {}, {}),
    ("unemployment in each country of the euro area", {}, {}),
    ("highest inflation in the euro area", {}, {}),
    # Two regions -> ambiguous entity, never guessed.
    ("euro area and EU inflation", {}, {}),
    # Directional trade geography: country would be consumed as REPORTER.
    ("exports to the EU", {"partner": "EU"}, {}),
    ("EU exports", {"flow": "EXPORT"}, {}),
    # Parser structure says comparison / decomposition.
    ("euro area inflation rate", {}, {"queryType": "comparison"}),
    ("euro area inflation rate", {}, {"needsDecomposition": True}),
    # Groups without an established aggregate keep asking.
    ("G7 GDP growth", {}, {}),
    ("BRICS inflation", {}, {}),
    ("ASEAN GDP", {}, {}),
    ("Nordic unemployment", {}, {}),
    # No region at all (World/Chinese are A1/A3 territory, not A2).
    ("World Bank GDP", {}, {}),
    ("欧元区失业率", {}, {}),
])
def test_fill_is_vetoed_without_inventing_scope(query, params, extra):
    intent = _intent(query, params, **extra)
    _svc()._apply_country_overrides(intent, query)
    assert not canonical_aggregate_region(intent.parameters.get("country"))
    assert not any(
        canonical_aggregate_region(value) for value in (intent.parameters.get("countries") or [])
    )


@pytest.mark.parametrize("params", [
    {"country": "France"},
    {"country": "Euro area"},
    {"countries": ["AT", "BE", "DE"]},
    {"countries": ["Euro area", "US"]},
])
def test_parser_set_geography_is_never_overridden(params):
    intent = _intent("euro area inflation rate", params)
    before = intent.model_copy(deep=True)
    _svc()._apply_country_overrides(intent, intent.originalQuery)
    assert intent == before


def test_member_veto_reuses_existing_markers_and_union_is_unchanged():
    svc = _svc()
    for query in ("euro area inflation as a whole", "overall EU GDP", "combined euro area output"):
        assert has_explicit_group_scope(svc, query)
        assert not has_explicit_member_group_scope(svc, query)
    for query in ("compare euro area members", "EU countries GDP", "inflation by country in the EU"):
        assert has_explicit_group_scope(svc, query)
        assert has_explicit_member_group_scope(svc, query)


@pytest.mark.parametrize("query", ["G7 GDP growth", "BRICS inflation", "ASEAN GDP growth"])
def test_groups_without_established_aggregate_still_ask(query):
    svc = _svc()
    svc._normalize_provider_alias = lambda value: value
    svc._detect_explicit_provider = lambda _query: None
    intent = _intent(query, {}, provider="WORLDBANK")
    svc._apply_country_overrides(intent, query)
    out = build_group_scope_clarification(svc, "agg-fill-ask", query, intent, False)
    assert out is not None and out.clarificationNeeded


@pytest.mark.asyncio
@pytest.mark.parametrize("query,routed,expected_region", [
    ("euro area inflation rate", "EUROSTAT", "EUROZONE"),
    ("eurozone unemployment rate", "EUROSTAT", "EUROZONE"),
    ("EU GDP growth", "EUROSTAT", "EU"),
    ("euro area inflation rate", "WORLDBANK", "EUROZONE"),
    ("EU GDP growth", "WORLDBANK", "EU"),
    ("euro area inflation rate", "IMF", "EUROZONE"),
    ("EU GDP growth", "IMF", "EU"),
    ("ECB policy rate euro area", "BIS", "EUROZONE"),
    ("euro area inflation rate", "FRED", "EUROZONE"),
])
async def test_filled_entity_routes_to_provider_native_identity_and_is_cached(
    monkeypatch, query, routed, expected_region,
):
    svc = _svc()
    parsed = _intent(query, {}, provider=routed)
    svc.openrouter = SimpleNamespace(parse_query=AsyncMock(return_value=parsed))
    svc._select_routed_provider = AsyncMock(return_value=routed)
    monkeypatch.setattr("backend.services.query_pipeline.unified_validate_routing", lambda *args: None)

    result = await QueryPipeline(svc).parse_and_route(query)
    country = result.intent.parameters.get("country")
    assert canonical_aggregate_region(country) == expected_region
    native = PROVIDER_GROUP_AGGREGATES.get(routed, {}).get(expected_region)
    if native:
        assert country == native  # the provider's declared id, not a member list
    assert not result.intent.parameters.get("countries")

    # The intent cache stores the post-fill parse (STORE happens on this result).
    key = f"agg-fill-test::{query}::{routed}"
    _put_cached_parse_result(key, result)
    cached = _get_cached_parse_result(key)
    assert canonical_aggregate_region(cached.intent.parameters.get("country")) == expected_region


def test_region_as_series_retrieval_text_keeps_the_aggregate():
    intent = SimpleNamespace(parameters={"country": "euro area"})
    text = country_qualified_indicator_text(intent, "FRED", "inflation rate")
    assert canonical_aggregate_region(text.replace("inflation rate", "").strip()) == "EUROZONE"
    # Concrete non-home countries are unchanged.
    assert country_qualified_indicator_text(
        SimpleNamespace(parameters={"country": "France"}), "FRED", "inflation rate"
    ) == "inflation rate France"


# --------------------------------------------------------------------------
# A1: parse-prompt example uses labels the aggregate identity table accepts
# --------------------------------------------------------------------------

def test_prompt_group_aggregate_example_names_resolve_to_entities():
    prompt = SimplifiedPrompt.generate()
    assert '"欧元区失业率" -> country "Euro area"' in prompt
    assert '"欧盟GDP" -> country "European Union"' in prompt
    assert canonical_aggregate_region("Euro area") == "EUROZONE"
    assert canonical_aggregate_region("European Union") == "EU"


# --------------------------------------------------------------------------
# A3: provider aggregates never member-expand; defaults are disclosed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["EUROSTAT", "WORLDBANK", "IMF", "BIS"])
@pytest.mark.parametrize("label", ["Euro area", "European Union", "World"])
def test_aggregate_entity_resolves_to_native_id_or_fails_honestly(provider, label):
    region = canonical_aggregate_region(label)
    normalized = normalize_aggregate_geography(provider, {"country": label})["country"]
    try:
        native = data_fetcher._native_aggregate_or_none(provider, normalized)
    except DataNotAvailableError:
        assert region not in PROVIDER_GROUP_AGGREGATES.get(provider, {})
        return
    assert native == normalized == PROVIDER_GROUP_AGGREGATES[provider][region]


def _imf_svc(resolve_countries=None):
    batch = AsyncMock(side_effect=lambda **kw: [_series(c, source="IMF") for c in kw["countries"]])
    provider = SimpleNamespace(
        fetch_batch_indicator=batch,
        _resolve_countries=Mock(side_effect=resolve_countries or (lambda item: [str(item)])),
    )
    return SimpleNamespace(imf_provider=provider), batch


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["European Union", "EU", "Euro area", "World"])
async def test_imf_aggregate_is_requested_as_one_native_entity(label):
    svc, batch = _imf_svc(resolve_countries=lambda item: ["AUT", "BEL", "DEU"])
    params = normalize_aggregate_geography("IMF", {"country": label, "indicator": "PCPIPCH"})
    intent = _intent(f"{label} inflation", params, provider="IMF")
    result = await data_fetcher._fetch_from_imf(svc, intent, dict(params), None)
    requested = batch.await_args.kwargs["countries"]
    assert len(requested) == 1 and canonical_aggregate_region(requested[0])
    svc.imf_provider._resolve_countries.assert_not_called()
    assert all(series.metadata.geoScopeAuthority is None for series in result)


@pytest.mark.asyncio
async def test_imf_null_geography_default_is_disclosed_not_silent():
    svc, batch = _imf_svc()
    intent = _intent("inflation", {"indicator": "PCPIPCH"}, provider="IMF")
    result = await data_fetcher._fetch_from_imf(svc, intent, {"indicator": "PCPIPCH"}, None)
    assert result and all(series.metadata.geoScopeAuthority == "default" for series in result)
    assert any("default scope" in note for note in result[0].metadata.notes)


@pytest.mark.asyncio
async def test_bis_aggregate_without_native_id_fails_honestly_and_euro_area_uses_native():
    fetch = AsyncMock(return_value=[_series("XM", source="BIS")])
    svc = SimpleNamespace(bis_provider=SimpleNamespace(fetch_indicator=fetch))
    plan = SimpleNamespace(provider="BIS", params=normalize_aggregate_geography(
        "BIS", {"country": "euro area", "indicator": "WS_CBPOL"},
    ), provider_request={})
    intent = _intent("ECB policy rate euro area", plan.params, provider="BIS")
    with patch.object(data_fetcher, "_assert_provider_map_authority", lambda *a, **k: None):
        await data_fetcher.fetch_from_provider_dispatch(svc, intent, plan)
        assert canonical_aggregate_region(fetch.await_args.kwargs["country"]) == "EUROZONE"
        assert not fetch.await_args.kwargs["countries"]

        eu_plan = SimpleNamespace(provider="BIS", params={"country": "European Union", "indicator": "WS_CBPOL"},
                                  provider_request={})
        with pytest.raises(DataNotAvailableError):
            await data_fetcher.fetch_from_provider_dispatch(svc, intent, eu_plan)


def _eurostat_svc():
    fetch = AsyncMock(side_effect=lambda **kw: _series(kw["country"]))
    return SimpleNamespace(eurostat_provider=SimpleNamespace(fetch_indicator=fetch)), fetch


@pytest.mark.asyncio
async def test_eurostat_null_geography_default_is_stamped_and_requested_scope_is_not():
    svc, fetch = _eurostat_svc()
    intent = _intent("欧元区失业率", {"indicator": "tipsun20"}, language="zh")
    result = await data_fetcher._fetch_from_eurostat(svc, intent, {"indicator": "tipsun20"}, None)
    assert result[0].metadata.geoScopeAuthority == "default"
    assert any("default scope" in note for note in result[0].metadata.notes)

    native = normalize_aggregate_geography("EUROSTAT", {"country": "Euro area"})["country"]
    requested = await data_fetcher._fetch_from_eurostat(
        svc, intent, {"indicator": "tipsun20", "country": native}, None,
    )
    assert requested[0].metadata.geoScopeAuthority is None
    # The aggregate entity is fetched as one entity, never member-expanded.
    assert canonical_aggregate_region(fetch.await_args.kwargs["country"]) == "EUROZONE"
    assert fetch.await_count == 2


@pytest.mark.parametrize("language,needle", [("zh", "默认范围"), ("en", "default scope")])
def test_default_geography_disclosure_is_localized_and_corrects_label(language, needle):
    series = _series("EU27_2020")
    data_fetcher._mark_provider_default_geography([series], "EU27_2020")
    intent = _intent(
        "欧元区失业率",
        {"__semantic_indicator_label": "unemployment rate", "__display_indicator_label": "欧元区失业率"},
        language=language,
    )
    response = QueryResponse(conversationId="c", intent=intent, data=[series], clarificationNeeded=False)
    out = _svc()._disclose_default_geography(response)
    assert needle in out.message and "EU27_2020" in out.message
    assert out.intent.parameters["__geo_scope_authority"] == "default"
    assert out.intent.parameters["__display_indicator_label"] == "unemployment rate"
    # Disclosure only: the served data is unchanged, and it is idempotent.
    assert out.data[0].metadata.country == "EU27_2020"
    message = out.message
    assert _svc()._disclose_default_geography(out).message == message


def test_requested_geography_response_gets_no_default_disclosure():
    intent = _intent("euro area inflation", {"country": "EA20"})
    response = QueryResponse(conversationId="c", intent=intent, data=[_series("EA20")], clarificationNeeded=False)
    out = _svc()._disclose_default_geography(response)
    assert not out.message
    assert "__geo_scope_authority" not in out.intent.parameters


def test_geo_scope_provenance_is_not_data_cache_identity():
    svc = _svc()
    svc.CACHE_KEY_VERSION = getattr(QueryService, "CACHE_KEY_VERSION", "v")
    base = {"indicator": "tipsun20", "startDate": "2020-01-01"}
    assert svc._build_cache_params("EUROSTAT", base) == svc._build_cache_params(
        "EUROSTAT", {**base, "__geo_scope_authority": "default"},
    )


# --------------------------------------------------------------------------
# B: group-scope follow-up executes the substituted original request
# --------------------------------------------------------------------------

@pytest.mark.parametrize("original,region,countries,expected", [
    ("euro area inflation rate", "EUROZONE", ["France"], "France inflation rate"),
    ("inflation in the euro area", "EUROZONE", ["France"], "inflation in France"),
    ("eurozone unemployment", "EUROZONE", ["Italy"], "Italy unemployment"),
    ("G7 GDP growth", "G7", ["Japan", "Canada"], "Japan and Canada GDP growth"),
    ("GDP growth for BRICS", "BRICS", ["India"], "GDP growth for India"),
])
def test_follow_up_query_substitutes_the_region(original, region, countries, expected):
    refined = build_group_scope_follow_up_query(original, region, countries)
    assert refined == expected
    assert CountryResolver.detect_regions_in_query(refined) == []


def test_follow_up_query_falls_back_to_indicator_plus_entity():
    refined = build_group_scope_follow_up_query(
        "inflation in the euro zone", "EUROZONE", ["France"], indicator_label="inflation rate",
    )
    assert refined == "inflation rate France"


def test_pending_group_scope_follow_up_scores_and_labels_the_refined_request():
    service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    conv_id = conversation_manager.get_or_create("conv-agg-fill-follow-up")
    pending_intent = ParsedIntent(
        apiProvider="EUROSTAT",
        indicators=["inflation rate"],
        parameters={"indicator": "TEC00118", "__display_indicator_label": "euro area inflation rate"},
        clarificationNeeded=False,
        originalQuery="euro area inflation rate",
    )
    conversation_manager.add_message_safe(conv_id, "user", "euro area inflation rate", intent=pending_intent)
    conversation_manager.set_pending_semantic_clarification(conv_id, {
        "kind": "group_scope",
        "region": "EUROZONE",
        "original_query": "euro area inflation rate",
        "question_lines": ["Choose scope"],
        "options": [
            {"id": "1", "label": "compare member countries", "value": "compare inflation rate across euro area member countries"},
            {"id": "2", "label": "one overall group value (aggregate/average if available)", "value": "inflation rate for the euro area group as a whole"},
        ],
    })
    fr = _series("France")
    recover = AsyncMock(return_value=None)
    uncertain = Mock(return_value=None)
    with patch.object(service, "_fetch_data", new_callable=AsyncMock, return_value=[fr]), \
         patch.object(service, "_build_post_parse_clarification", new_callable=AsyncMock, return_value=None), \
         patch.object(service.pipeline, "validate_intent", return_value=ValidationResult(
             is_multi_indicator=False, is_valid=True, validation_error=None,
             suggestions=None, is_confident=True, confidence_reason=None,
         )), \
         patch.object(service, "_maybe_recover_from_uncertain_match", recover), \
         patch.object(service, "_maybe_improve_country_coverage", new_callable=AsyncMock, return_value=([fr], None)), \
         patch.object(service, "_build_uncertain_result_clarification", uncertain), \
         patch.object(service, "_verify_execution_result", new_callable=AsyncMock, return_value=None):
        response = asyncio.run(service.process_query("what about France", conversation_id=conv_id))

    assert not response.clarificationNeeded and response.data
    params = response.intent.parameters
    assert params.get("country") == "FR"
    for text in (response.intent.originalQuery, params.get("__original_query"), params.get("__display_indicator_label")):
        assert "France" in text and "inflation" in text
        assert CountryResolver.detect_regions_in_query(text) == []
    scored = [str(arg) for call in recover.await_args_list + uncertain.call_args_list
              for arg in list(call.args) + list(call.kwargs.values()) if isinstance(arg, str)]
    assert "what about France" not in scored
    assert conversation_manager.get_pending_semantic_clarification(conv_id) is None


# --------------------------------------------------------------------------
# C1: blank-message clarification gets readable text
# --------------------------------------------------------------------------

def test_blank_message_clarification_gets_rendered_question_and_options():
    response = QueryResponse(
        conversationId="c",
        clarificationNeeded=True,
        clarificationQuestions=["Which scope do you mean?", "1. compare member countries"],
        clarificationOptions=[
            ClarificationOption(id="1", label="compare member countries", value="a"),
            ClarificationOption(id="2", label="one overall group value", value="b"),
        ],
    )
    out = _svc()._finalize_empty_data_response(response)
    assert out.message.splitlines() == [
        "Which scope do you mean?", "1. compare member countries", "2. one overall group value",
    ]
    assert out.clarificationNeeded and out.clarificationQuestions == response.clarificationQuestions
    assert _svc()._finalize_empty_data_response(out).message == out.message


def test_clarification_with_message_is_left_alone():
    response = QueryResponse(
        conversationId="c", clarificationNeeded=True,
        clarificationQuestions=["Q?"], message="Custom text",
    )
    assert _svc()._finalize_empty_data_response(response).message == "Custom text"
