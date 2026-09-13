"""Parsed geographic entities survive routing, caching, fetching and verification."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.providers.worldbank import WorldBankProvider
from backend.routing.country_resolver import CountryResolver
from backend.services.data_fetcher import materialize_execution_plan
from backend.services.execution_planner import build_minimal_execution_plan
from backend.services.geography_validation import assess_country_coverage
from backend.services.indicator_clarification import build_group_scope_clarification
from backend.services.query import QueryService
from backend.services.query_pipeline import QueryPipeline
from backend.services.query_decomposition import maybe_expand_ranking_country_scope
from backend.utils.retry import DataNotAvailableError


def request(params, provider="WorldBank", query="GDP in 2023", clarify=False):
    return ParsedIntent(
        apiProvider=provider, indicators=["GDP"],
        parameters={"startDate": "2023-01-01", "endDate": "2023-12-31", **params},
        originalQuery=query, clarificationNeeded=clarify,
        clarificationQuestions=["Which geographic scope do you mean?"] if clarify else [],
        confidence=0.96,
    )


@pytest.mark.parametrize("params,query", [
    ({"country": "European Union"}, "GDP for the European Union as a whole"),
    ({"country": "Euro area"}, "GDP for the euro area as a whole"),
    ({"country": "EUU"}, "GDP for the European Union as a whole"),
    ({"countries": ["US", "European Union"]}, "Compare US and European Union GDP"),
    ({"countries": ["European Union", "France", "Germany"]}, "Compare European Union, France and Germany GDP"),
    ({"countries": ["France", "Germany"]}, "Compare France and Germany, two EU member countries"),
    ({"countries": sorted(CountryResolver.EU_MEMBERS)}, "Compare all EU member countries"),
    ({"country": "Germany"}, "Germany GDP from Eurostat"),
])
def test_country_overrides_preserve_explicit_structured_entities(params, query):
    svc = QueryService.__new__(QueryService)
    intent = request(params, query=query)
    before = intent.model_copy(deep=True)
    svc._apply_country_overrides(intent, query)
    assert intent == before


@pytest.mark.parametrize("params", [{}, {"country": "US"}, {"countries": ["US"]}])
def test_country_fallback_still_fills_missing_or_default_context(params):
    svc = QueryService.__new__(QueryService)
    intent = request(params, query="Compare France and Germany GDP")
    svc._apply_country_overrides(intent, intent.originalQuery)
    assert intent.parameters["countries"] == ["FR", "DE"]
    assert not intent.parameters.get("country")


@pytest.mark.parametrize("country", ["European Union", "Euro area", "Europe", "G20"])
def test_parser_scope_question_is_not_dismissed_or_rewritten(country):
    svc = QueryService.__new__(QueryService)
    intent = request({"country": country}, query=f"GDP in {country}", clarify=True)
    before = intent.model_copy(deep=True)
    svc._apply_country_overrides(intent, intent.originalQuery)
    assert not svc._maybe_resolve_region_clarification(intent.originalQuery, intent)
    assert build_group_scope_clarification(svc, "aggregate-clarification", intent.originalQuery, intent, False) is None
    assert intent == before


@pytest.mark.asyncio
@pytest.mark.parametrize("parsed_provider,routed_provider,country,expected", [
    ("WorldBank", "WORLDBANK", "European Union", "EUU"),
    ("WorldBank", "WORLDBANK", "Euro area", "EMU"),
    ("WorldBank", "EUROSTAT", "European Union", "EU27_2020"),
    ("Eurostat", "WORLDBANK", "Euro area", "EMU"),
    ("WorldBank", "WORLDBANK", "European_Union", "EUU"),
    ("WorldBank", "WORLDBANK", "EU member countries", "EU member countries"),
    ("WorldBank", "WORLDBANK", "Europe", "Europe"),
    ("WorldBank", "FRED", "European Union", "European Union"),
])
async def test_real_parse_route_normalizes_only_exact_entities_after_routing(
    monkeypatch, parsed_provider, routed_provider, country, expected,
):
    svc = QueryService.__new__(QueryService)
    intent = request({"country": country}, provider=parsed_provider, query=f"GDP in {country}")
    svc.openrouter = SimpleNamespace(parse_query=AsyncMock(return_value=intent))
    svc._select_routed_provider = AsyncMock(return_value=routed_provider)
    monkeypatch.setattr("backend.services.query_pipeline.unified_validate_routing", lambda *args: None)
    parsed = await QueryPipeline(svc).parse_and_route(intent.originalQuery)
    assert parsed.intent.apiProvider == routed_provider
    assert parsed.intent.parameters["country"] == expected
    assert not parsed.intent.parameters.get("countries")
    assert parsed.intent.parameters["startDate"] == "2023-01-01"
    assert parsed.intent.parameters["endDate"] == "2023-12-31"


@pytest.mark.parametrize("params,expected", [
    ({"country": "European Union"}, ["EUU"]),
    ({"country": "Euro area"}, ["EMU"]),
    ({"countries": ["US", "European Union"]}, ["US", "EUU"]),
    ({"countries": ["European Union", "France", "Germany"]}, ["EUU", "France", "Germany"]),
    ({"countries": sorted(CountryResolver.EU_MEMBERS)}, sorted(CountryResolver.EU_MEMBERS)),
])
def test_shared_materialization_preserves_entity_shape_in_dispatch_and_cache(params, expected):
    intent = request({**params, "indicator": "NY.GDP.MKTP.CD", "sex": "T"})
    plan = build_minimal_execution_plan(intent.originalQuery, intent)
    result = materialize_execution_plan(plan, provider="WORLDBANK", intent=intent, params=intent.parameters)
    assert result.provider_request["country_scope"] == expected
    assert result.cache_identity["provider_request"]["country_scope"] == expected
    assert result.expected_shape["requested_countries"] == expected
    assert result.expected_shape["min_series_count"] == len(expected)
    assert result.params["sex"] == "T"
    assert result.params["startDate"] == "2023-01-01"


def series(country, value=100.0):
    return NormalizedData(
        metadata=Metadata(
            source="World Bank", indicator="GDP (current US$)", country=country,
            frequency="annual", unit="current US$", seriesId="NY.GDP.MKTP.CD",
        ),
        data=[{"date": "2023-01-01", "value": value}],
    )


@pytest.mark.parametrize("requested,returned", [
    (["EUU"], ["European Union"]), (["EMU"], ["Euro area"]),
    (["US", "EUU"], ["United States", "European Union"]),
    (["EUU", "FR", "DE"], ["European Union", "France", "Germany"]),
])
def test_verification_recognizes_same_native_aggregate_and_provider_label(requested, returned):
    svc = QueryService.__new__(QueryService)
    intent = request({"countries": requested})
    plan = build_minimal_execution_plan(intent.originalQuery, intent)
    assert svc._verify_execution_plan_structure(plan, [series(country) for country in returned]) is None


@pytest.mark.parametrize("requested,returned", [
    (["EUU"], ["France"]), (["EUU"], ["Euro area"]),
    (["EUU"], ["Europe & Central Asia"]),
    (["EUU", "FR", "DE"], ["France", "Germany"]),
    (["FR", "DE"], ["European Union"]),
])
def test_verification_still_rejects_wrong_or_missing_geographic_entities(requested, returned):
    svc = QueryService.__new__(QueryService)
    intent = request({"countries": requested})
    plan = build_minimal_execution_plan(intent.originalQuery, intent)
    assert svc._verify_execution_plan_structure(plan, [series(country) for country in returned])


@pytest.mark.parametrize("returned,complete", [
    (["European Union", "France", "Germany"], True),
    (["France", "Germany"], False),
    (["Euro area", "France", "Germany"], False),
])
def test_coverage_and_cache_admission_do_not_drop_requested_aggregate(returned, complete):
    intent = request({"countries": ["EUU", "France", "Germany"]})
    data = [series(country) for country in returned]
    coverage = assess_country_coverage(intent, data)
    assert coverage is not None and coverage["requested_count"] == 3
    assert coverage["complete"] is complete
    svc = QueryService.__new__(QueryService)
    assert svc._cached_result_has_complete_country_coverage(intent.parameters, data) is complete


@pytest.mark.parametrize("returned,complete", [
    ("European Union", True), ("France", False), ("Euro area", False),
])
def test_single_aggregate_cache_admission_requires_that_entity(returned, complete):
    svc = QueryService.__new__(QueryService)
    assert svc._cached_result_has_complete_country_coverage(
        {"country": "EUU"}, [series(returned)],
    ) is complete


@pytest.mark.asyncio
async def test_member_payload_for_aggregate_is_not_written_to_either_cache(monkeypatch):
    redis = AsyncMock()
    memory = Mock()
    monkeypatch.setattr("backend.services.query.get_redis_cache", redis)
    monkeypatch.setattr("backend.services.query.cache_service.cache_data", memory)
    svc = QueryService.__new__(QueryService)
    await svc._save_to_cache("WORLDBANK", {"country": "EUU"}, [series("France")])
    redis.assert_not_awaited()
    memory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", ["EUU", "EMU"])
async def test_native_aggregate_no_data_never_requests_member_countries(monkeypatch, native):
    provider = WorldBankProvider.__new__(WorldBankProvider)
    provider.base_url = "https://api.worldbank.org/v2"
    provider.metadata_search = None
    provider._indicator_source_id = lambda indicator: None
    calls = []

    async def no_data(client, url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json=[{"pages": 1}, []], request=httpx.Request("GET", url))

    provider._get_with_retry = no_data
    monkeypatch.setattr("backend.providers.worldbank.get_http1_client", lambda: object())
    with pytest.raises(DataNotAvailableError):
        await provider.fetch_indicator("NY.GDP.MKTP.CD", country=native, _skip_alternatives=True)
    assert calls and all(f"/country/{native}/indicator/" in url for url in calls)


@pytest.mark.parametrize("native,provider,expected", [
    ("EUU", "EUROSTAT", "EU27_2020"), ("EMU", "EUROSTAT", "EA20"),
    ("EA20", "WORLDBANK", "EMU"),
    ("EUU", "FRED", "European Union"), ("EA20", "FRED", "euro area"),
])
def test_provider_change_rematerializes_native_scope_before_cache(native, provider, expected):
    intent = request({"country": native})
    plan = build_minimal_execution_plan(intent.originalQuery, intent)
    intent.apiProvider = provider
    materialized = materialize_execution_plan(plan, provider=provider, intent=intent, params=intent.parameters)
    assert materialized.params["country"] == expected
    assert materialized.provider_request["country_scope"] == [expected]
    assert materialized.expected_shape["requested_countries"] == [expected]
    assert materialized.cache_identity["provider_request"]["country_scope"] == [expected]


@pytest.mark.parametrize("clarify", [False, True])
def test_multi_concept_helper_preserves_scalar_aggregate_and_scope_question(clarify):
    svc = QueryService.__new__(QueryService)
    svc._infer_multi_concept_indicators_from_query = lambda query: ["GDP", "inflation"]
    intent = request({"country": "EUU"}, query="Compare GDP and inflation for the European Union as a whole", clarify=clarify)
    before = intent.model_copy(deep=True)
    assert not svc._maybe_expand_multi_concept_intent(intent.originalQuery, intent)
    assert intent == before


def test_ranking_scope_does_not_replace_explicit_aggregate():
    params = {"country": "EU27_2020"}
    assert maybe_expand_ranking_country_scope("rank the European Union GDP across years", "EUROSTAT", params) == params


@pytest.mark.asyncio
async def test_empty_data_recovery_preserves_scalar_aggregate(monkeypatch):
    svc = QueryService.__new__(QueryService)
    svc._build_distilled_indicator_query = lambda query: "GDP"
    svc._select_routed_provider = AsyncMock(return_value="WORLDBANK")
    svc._fetch_data = AsyncMock(return_value=[])
    intent = request({"country": "EUU"}, query="Compare European Union GDP across years")
    await svc._maybe_recover_from_empty_data(intent.originalQuery, intent)
    retried = svc._fetch_data.await_args.args[0]
    assert retried.parameters["country"] == "EUU"
    assert not retried.parameters.get("countries")


@pytest.mark.asyncio
async def test_real_preflight_never_partitions_only_a_subset_of_entities():
    svc = QueryService.__new__(QueryService)
    svc._provider_covers_country_list = lambda provider, countries: False
    svc._select_indicator_query_for_resolution = lambda intent: "GDP"
    svc._get_provider_for_single_country = lambda code, query, provider: ("FRED" if code == "US" else "WORLDBANK", None)
    svc._fetch_data = AsyncMock(return_value=[])
    intent = request({"countries": ["EUU", "US", "Germany"]})
    result = await svc._preflight_geographic_split(intent)
    assert result is None
    svc._fetch_data.assert_not_awaited()


@pytest.mark.parametrize("params", [
    {"country": "EUU"}, {"countries": ["US", "EUU"]},
    {"countries": ["EUU", "France", "Germany"]}, {"countries": ["France", "Germany"]},
])
def test_later_group_clarification_does_not_replace_structured_entities(params):
    svc = QueryService.__new__(QueryService)
    intent = request(params, query="GDP in the European Union")
    before = intent.model_copy(deep=True)
    assert build_group_scope_clarification(svc, "preserve-entities", intent.originalQuery, intent, False) is None
    assert intent == before


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_path", ["pipeline", "shared_fetch", "standard_fallback"])
@pytest.mark.parametrize("params,query,expected", [
    ({"country": "European Union"}, "GDP for the European Union as a whole in 2023 from the World Bank", ["EUU"]),
    ({"country": "Euro area"}, "GDP for the euro area as a whole in 2023 from the World Bank", ["EMU"]),
    ({"countries": ["US", "European Union"]}, "Compare US and European Union GDP in 2023 from the World Bank", ["USA", "EUU"]),
    ({"countries": ["European Union", "France", "Germany"]}, "Compare European Union, France and Germany GDP in 2023 from the World Bank", ["EUU", "FRA", "DEU"]),
    ({"countries": ["France", "Germany"]}, "Compare France and Germany, two EU member countries, GDP in 2023 from the World Bank", ["FRA", "DEU"]),
])
async def test_actual_parse_plan_fetch_and_validation_preserve_entities(monkeypatch, params, query, expected, entry_path):
    svc = QueryService.__new__(QueryService)
    intent = request(params, query=query)
    svc.openrouter = SimpleNamespace(parse_query=AsyncMock(return_value=intent))
    svc._select_routed_provider = AsyncMock(return_value="WORLDBANK")
    svc._preflight_geographic_split = AsyncMock(return_value=None)
    svc._get_from_cache = AsyncMock(return_value=None)
    svc._save_to_cache = AsyncMock()
    svc._normalize_bis_metadata_labels = lambda data: None

    async def resolve(provider, parsed, current):
        return {**current, "indicator": "NY.GDP.MKTP.CD", "__semantic_authority": "llm_adjudication"}

    svc._resolve_indicator_for_fetch = AsyncMock(side_effect=resolve)
    provider = WorldBankProvider.__new__(WorldBankProvider)
    provider.base_url = "https://api.worldbank.org/v2"
    provider.metadata_search = None
    provider._indicator_source_id = lambda indicator: None
    svc.world_bank_provider = provider
    calls = []
    labels = {"EUU": "European Union", "EMU": "Euro area", "USA": "United States", "FRA": "France", "DEU": "Germany"}
    values = {"EUU": 18659279022583.3, "EMU": 15957338405375.8, "USA": 27720709000000.0, "FRA": 3056250648138.29, "DEU": 4562207532490.28}

    async def http_boundary(client, url, **kwargs):
        calls.append((url, copy.deepcopy(kwargs)))
        codes = url.split("/country/", 1)[1].split("/indicator/", 1)[0].split(";")
        records = []
        for code in codes:
            iso = CountryResolver.to_iso3(code) or code
            records.append({
                "indicator": {"id": "NY.GDP.MKTP.CD", "value": "GDP (current US$)"},
                "country": {"id": code, "value": labels.get(iso, iso)},
                "countryiso3code": iso, "date": "2023", "value": values.get(iso, 1.0),
            })
        return httpx.Response(200, json=[{"pages": 1}, records], request=httpx.Request("GET", url))

    provider._get_with_retry = http_boundary
    monkeypatch.setattr("backend.providers.worldbank.get_http1_client", lambda: object())
    monkeypatch.setattr("backend.services.query_pipeline.unified_validate_routing", lambda *args: None)
    if entry_path == "standard_fallback":
        svc.pipeline = QueryPipeline(svc)
        svc.settings = SimpleNamespace(use_minimal_execution_plan=True, use_post_fetch_semantic_judge=False)
        svc._build_multi_concept_query_clarification = lambda **kwargs: None
        svc._rerank_data_by_query_relevance = lambda query, data: data
        svc._apply_ranking_projection = lambda query, data: data
        svc._maybe_recover_from_uncertain_match = AsyncMock(return_value=None)
        svc._build_uncertain_result_clarification = lambda **kwargs: None
        svc._has_implausible_top_series = lambda query, data: False
        svc._build_alternative_series = lambda intent, data: None
        response = await svc._standard_query_processing(query, "aggregate-standard-fallback")
        assert not response.error and not response.clarificationNeeded
        intent = response.intent
        data = response.data
        plan = build_minimal_execution_plan(query, intent)
    else:
        if entry_path == "pipeline":
            parsed = await QueryPipeline(svc).parse_and_route(query)
            intent = parsed.intent
        # The data-agent path reapplies the same helper after parsing.
        svc._apply_country_overrides(intent, query)
        assert build_group_scope_clarification(svc, "full-plumbing", query, intent, False) is None
        plan = build_minimal_execution_plan(query, intent)
        data = await svc._fetch_data(intent, execution_plan=plan)
    assert len(data) == len(expected)
    observed = {row.metadata.country: row.data[0].value for row in data}
    assert observed == {labels[code]: values[code] for code in expected}
    assert all(row.metadata.unit == "current US$" and row.data[0].date == "2023-01-01" for row in data)
    assert all(row.metadata.seriesId == "NY.GDP.MKTP.CD" for row in data)
    assert all(kwargs["params"]["date"] == "2023:2023" for _, kwargs in calls)
    assert svc._verify_execution_plan_structure(plan, data) is None
    cached_scope = svc._get_from_cache.await_args.args[1]["__execution_plan_identity"]["provider_request"]["country_scope"]
    assert [CountryResolver.to_iso3(CountryResolver.normalize(code) or code) or code for code in cached_scope] == expected
