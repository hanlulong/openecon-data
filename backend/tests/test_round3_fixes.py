"""Guards for the round-3 framework fixes (routing coverage, cache TTL
coherence, export bounds, response-status telemetry, finalizer whitespace,
CoinGecko frequency-from-spacing)."""

import inspect

import pytest

from backend.models import ExportRequest, NormalizedData, ParsedIntent, QueryResponse
from backend.providers.coingecko import CoinGeckoProvider
from backend.services.provider_fallback import provider_covers_country_list


def _series(n_points=3):
    return NormalizedData.model_validate(
        {
            "metadata": {
                "source": "FRED",
                "indicator": "Test",
                "country": "US",
                "frequency": "monthly",
                "unit": "Percent",
                "lastUpdated": "2026-01-01",
                "seriesId": "TEST",
                "apiUrl": "https://example.com",
            },
            "data": [{"date": f"2020-{i%12+1:02d}-01", "value": 1.0} for i in range(n_points)],
        }
    )


# --- T3: single-country routing coverage -----------------------------------

def test_capability_check_rejects_single_country_mismatch():
    # CATALOG TRUTH (2026-07-19, correctness beats provider preference):
    # FRED genuinely carries France-titled international mirrors, so
    # "covers France" is TRUE — the old US-only assertion encoded the false
    # premise that force-downgraded correct monthly data to annual providers.
    assert provider_covers_country_list("FRED", ["France"]) is True
    assert provider_covers_country_list("FRED", ["US"]) is True
    assert provider_covers_country_list("STATSCAN", ["Germany"]) is False
    assert provider_covers_country_list("WORLDBANK", ["France"]) is True
    # The rejection MECHANISM still works when the catalog is truly empty:
    from unittest.mock import patch

    with patch(
        "backend.services.provider_fallback._fred_catalog_covers_country",
        side_effect=lambda code: code == "US",
    ):
        assert provider_covers_country_list("FRED", ["France"]) is False


def test_routing_gate_no_longer_requires_multiple_countries():
    from backend.services.query import QueryService

    src = inspect.getsource(QueryService._select_routed_provider)
    assert "len(countries) > 1 and not self._provider_covers_country_list" not in src
    assert "_provider_covers_country_list(routed_provider, countries)" in src


# --- T4: coherent cache TTL --------------------------------------------------

def test_cache_data_honors_explicit_ttl(monkeypatch):
    from backend.services.cache import CacheService

    svc = CacheService()
    captured = {}
    monkeypatch.setattr(svc, "set", lambda key, value, ttl=None: captured.update(ttl=ttl))
    svc.cache_data("FRED", {"a": 1}, [_series()], ttl=1234)
    assert captured["ttl"] == 1234
    # Without an explicit ttl the frequency path still applies.
    svc.cache_data("FRED", {"a": 1}, [_series()])
    assert captured["ttl"] == svc._ttl_for_frequency("monthly")


@pytest.mark.asyncio
async def test_redis_ttl_for_provider_lookup():
    from backend.services.redis_cache import RedisCacheService

    rc = RedisCacheService()
    assert rc.ttl_for_provider("COINGECKO") == rc.ttl_config["COINGECKO"]
    assert rc.ttl_for_provider("nope") == rc.ttl_config["default"]
    assert rc.ttl_for_provider("fred") == rc.ttl_config["FRED"]


# --- T9: export bounds -------------------------------------------------------

def test_export_request_bounds():
    ok = ExportRequest(data=[_series()], format="csv")
    assert len(ok.data) == 1
    with pytest.raises(Exception):
        ExportRequest(data=[_series() for _ in range(101)], format="csv")
    with pytest.raises(Exception):
        ExportRequest(data=[_series(n_points=200_001)], format="csv")


# --- telemetry: derive_response_status --------------------------------------

def test_derive_response_status_enum():
    from backend.main import derive_response_status

    def resp(**kw):
        base = dict(conversationId="c", clarificationNeeded=False)
        base.update(kw)
        return QueryResponse(**base)

    assert derive_response_status(resp(data=[_series()])) == "data"
    assert derive_response_status(resp(error="no_data_found")) == "error"
    assert derive_response_status(resp(clarificationNeeded=True)) == "clarification"
    assert derive_response_status(resp(message="explained")) == "messaged_no_data"
    assert derive_response_status(resp(registrationRequired=True)) == "registration_gate"
    assert derive_response_status(resp()) == "empty"
    assert derive_response_status(None) == "empty"


# --- finalizer: whitespace-only error is not an explanation ------------------

def test_finalizer_stamps_over_whitespace_error():
    from backend.services.query import QueryService

    svc = QueryService.__new__(QueryService)  # no init needed for the pure method
    intent = ParsedIntent(
        apiProvider="WORLDBANK",
        indicators=["GDP"],
        parameters={"country": "Tuvalu"},
        clarificationNeeded=False,
        originalQuery="GDP of Tuvalu",
    )
    response = QueryResponse(
        conversationId="c", clarificationNeeded=False, error=" ", intent=intent
    )
    out = svc._finalize_empty_data_response(response)
    assert (out.error or "").strip() == "no_data_found"
    assert "No Data Available" in (out.message or "")


# --- model guard: whitespace error can never escape from ANY setter ---------

@pytest.mark.parametrize("blank", [" ", "  ", "\t", "\n", "  \t\n "])
def test_query_response_normalizes_whitespace_error_to_none(blank):
    """A whitespace-only error is normalized to None at the model boundary,
    even when data is non-empty (which the empty-data finalizer skips)."""
    # empty-data shape
    r = QueryResponse(conversationId="c", clarificationNeeded=False, error=blank)
    assert r.error is None
    # non-empty-data shape — the finalizer early-returns here, so the model
    # validator is the only guard that runs.
    r2 = QueryResponse(
        conversationId="c", clarificationNeeded=False, error=blank, data=[_series()]
    )
    assert r2.error is None
    # a real error is left untouched
    r3 = QueryResponse(conversationId="c", clarificationNeeded=False, error="no_data_found")
    assert r3.error == "no_data_found"


def test_failed_indicator_choice_response_drops_blank_exception_error():
    """build_failed_indicator_choice_response must not surface error=str(exc)
    when the exception message is blank (the observed live whitespace source)."""
    from backend.services.indicator_clarification import (
        build_failed_indicator_choice_response,
    )
    from backend.services.query import QueryService

    svc = QueryService.__new__(QueryService)
    intent = ParsedIntent(
        apiProvider="WORLDBANK",
        indicators=["GDP"],
        clarificationNeeded=False,
        originalQuery="GDP of Tuvalu",
    )
    resp = build_failed_indicator_choice_response(
        qs=svc,
        conversation_id="c",
        query="GDP of Tuvalu",
        intent=intent,
        options=[],
        selected_option=None,
        question_lines=[],
        error=str(ValueError("   ")),  # blank exception message
    )
    assert resp.error is None


# --- CoinGecko: frequency from actual point spacing --------------------------

def test_coingecko_frequency_from_spacing():
    hour_ms = 60 * 60 * 1000
    hourly = [[i * hour_ms, 1.0] for i in range(50)]
    daily = [[i * 24 * hour_ms, 1.0] for i in range(50)]
    minutely = [[i * 5 * 60 * 1000, 1.0] for i in range(50)]
    f = CoinGeckoProvider._frequency_from_point_spacing
    assert f(hourly, 30) == "hourly"       # 8-90d windows were mislabeled daily
    assert f(daily, 365) == "daily"
    assert f(minutely, 1) == "5-minute"
    assert f([], 365) == "daily"           # fallback ladder


# --- Round 5: provider-native group aggregates -------------------------------

def test_group_scope_resolves_native_aggregate_instead_of_asking():
    """Post-651a078 contract: an EMPTY parsed geography naming one established
    aggregate is filled with that entity before routing (no redundant scope
    ask), the provider then sees its native id; parser-set geography — even a
    member list — is never overridden."""
    from backend.services.indicator_clarification import build_group_scope_clarification
    from backend.services.provider_strategy import (
        canonical_aggregate_region,
        normalize_aggregate_geography,
    )
    from backend.services.query import QueryService

    svc = QueryService.__new__(QueryService)
    for query, provider, region in (
        ("euro area inflation rate", "EUROSTAT", "EUROZONE"),
        ("eurozone unemployment rate", "EUROSTAT", "EUROZONE"),
        ("EU GDP growth", "EUROSTAT", "EU"),
        ("euro area inflation rate as a whole", "EUROSTAT", "EUROZONE"),
        ("euro area inflation rate", "WORLDBANK", "EUROZONE"),
        ("EU GDP growth", "WORLDBANK", "EU"),
        ("euro area inflation rate", "IMF", "EUROZONE"),
        ("euro area inflation rate", "FRED", "EUROZONE"),
    ):
        intent = ParsedIntent(
            apiProvider=provider, indicators=["inflation rate"], parameters={},
            clarificationNeeded=False, originalQuery=query,
        )
        svc._apply_country_overrides(intent, query)
        assert canonical_aggregate_region(intent.parameters.get("country")) == region, query
        out = build_group_scope_clarification(svc, "c1", query, intent, is_multi_indicator=False)
        assert out is None, query  # no redundant scope question
        routed = normalize_aggregate_geography(provider, intent.parameters)
        assert canonical_aggregate_region(routed["country"]) == region
        assert not routed.get("countries")

    # "as a whole" is the GROUP value — never the EU-27 default.
    intent = ParsedIntent(
        apiProvider="EUROSTAT", indicators=["inflation rate"], parameters={},
        clarificationNeeded=False, originalQuery="euro area inflation rate as a whole",
    )
    svc._apply_country_overrides(intent, intent.originalQuery)
    routed = normalize_aggregate_geography("EUROSTAT", intent.parameters)
    assert canonical_aggregate_region(routed["country"]) == "EUROZONE"

    # Parser-set geography (members, a country) is never overridden.
    for params in ({"countries": ["AT", "BE", "DE"]}, {"country": "France"}):
        intent = ParsedIntent(
            apiProvider="EUROSTAT", indicators=["inflation rate"], parameters=dict(params),
            clarificationNeeded=False, originalQuery="euro area inflation rate",
        )
        svc._apply_country_overrides(intent, intent.originalQuery)
        assert intent.parameters == params

    # A concrete country (US counts) or member-comparison wording: not filled.
    for query in ("US imports from the euro area", "compare euro area member countries inflation"):
        intent = ParsedIntent(
            apiProvider="EUROSTAT", indicators=["inflation rate"], parameters={},
            clarificationNeeded=False, originalQuery=query,
        )
        svc._apply_country_overrides(intent, query)
        assert not canonical_aggregate_region(intent.parameters.get("country")), query


def test_group_scope_without_native_aggregate_still_asks():
    from backend.services.indicator_clarification import build_group_scope_clarification
    from backend.services.query import QueryService

    svc = QueryService.__new__(QueryService)
    svc._normalize_provider_alias = lambda value: value
    svc._detect_explicit_provider = lambda _query: None
    for query in ("G7 gdp growth", "BRICS inflation", "ASEAN GDP growth"):
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["gdp growth"],
            parameters={},
            clarificationNeeded=False,
            originalQuery=query,
        )
        svc._apply_country_overrides(intent, query)
        assert not intent.parameters.get("country"), query  # no established aggregate
        out = build_group_scope_clarification(
            svc, "c1", query, intent, is_multi_indicator=False
        )
        # No official aggregate series -> the scope clarification still applies.
        assert out is not None and out.clarificationNeeded, query
