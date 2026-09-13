"""Clarification collection must share the selector's country constraint."""

import copy
from types import SimpleNamespace

import pytest

from backend.models import ParsedIntent
from backend.routing.country_resolver import CountryResolver
from backend.services import indicator_clarification as clarification
from backend.services.indicator_selector import IndicatorSelector
from backend.services.provider_strategy import collect_target_countries


USA = ("NYGDPPCAPKDUSA", "Constant GDP per capita for the United States")
CHINA = ("NYGDPPCAPKDCHN", "Constant GDP per capita for China")
GERMANY = ("NYGDPPCAPKDDEU", "Constant GDP per capita for Germany")
NEUTRAL = ("GDPPC", "Gross domestic product per capita")


@pytest.fixture
def collect(monkeypatch):
    # Catalog coverage and relevance are separate policies; keep them fixed so
    # the actual shared country helper and collector determine these results.
    monkeypatch.setattr(
        "backend.services.provider_fallback._fred_catalog_covers_country", lambda country: True,
    )
    monkeypatch.setattr(clarification, "_score_series_relevance", lambda *args: 0.0)
    qs = SimpleNamespace(
        _select_indicator_query_for_resolution=lambda intent: "GDP per capita",
        _collect_target_countries=collect_target_countries,
        _extract_countries_from_query=lambda query: [],
        _normalize_country_to_iso2=lambda country: (
            CountryResolver.normalize(country)
            if len(CountryResolver.normalize(country) or "") == 2 else None
        ),
        _get_fallback_providers=lambda *args, **kwargs: [],
        _detect_explicit_provider=lambda query: None,
    )

    def run(candidates, scores, parameters, provider="FRED"):
        before = copy.deepcopy((candidates, scores))
        monkeypatch.setattr(
            IndicatorSelector, "_get_candidates_with_scores",
            lambda self, query, current_provider, **kwargs: (
                (candidates, scores) if current_provider == provider else ([], [])
            ),
        )
        intent = ParsedIntent(
            apiProvider=provider, indicators=["GDP per capita"],
            parameters=parameters, originalQuery="GDP per capita", clarificationNeeded=False,
        )
        options = clarification.collect_indicator_choice_options(
            qs, intent.originalQuery, intent, max_options=6,
        )
        assert (candidates, scores) == before
        return [clarification.parse_indicator_option(option) for option in options]

    return run


def test_single_country_drops_higher_scored_wrong_countries_and_keeps_score_identity(collect):
    assert collect(
        [USA, NEUTRAL, CHINA, GERMANY], [0.99, 0.9, 0.4, 0.98], {"country": "CN"},
    ) == [("FRED", NEUTRAL[0]), ("FRED", CHINA[0])]


def test_all_conflicting_candidates_do_not_become_actionable_options(collect):
    assert collect([USA, GERMANY], [0.99, 0.98], {"country": "China"}) == []


def test_missing_score_preserves_neutral_candidate_with_existing_zero_default(collect):
    assert collect([USA, CHINA, NEUTRAL], [0.99, 0.8], {"country": "CN"}) == [
        ("FRED", CHINA[0]), ("FRED", NEUTRAL[0]),
    ]


def test_country_parameterized_neutral_concept_remains_available(collect):
    candidate = ("NY.GDP.PCAP.KD", "GDP per capita (constant US$)")
    assert collect([candidate], [0.9], {"country": "CN"}, provider="WORLDBANK") == [
        ("WORLDBANK", candidate[0]),
    ]


def test_malformed_short_candidate_keeps_existing_skip_behavior(collect):
    assert collect([(), ("incomplete",), CHINA], [0.99, 0.98, 0.8], {"country": "CN"}) == [
        ("FRED", CHINA[0]),
    ]


@pytest.mark.parametrize("parameters", [
    {}, {"country": "unresolved geography"}, {"countries": ["CN", "CA"]},
    {"countries": ["CN", "unresolved geography"]},
])
def test_missing_unresolved_or_multiple_country_scope_keeps_existing_collection(collect, parameters):
    assert collect([USA, CHINA], [0.9, 0.8], parameters) == [
        ("FRED", USA[0]), ("FRED", CHINA[0]),
    ]
