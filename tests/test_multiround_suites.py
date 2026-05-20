from __future__ import annotations

from datetime import datetime

import pytest

from scripts.multiround_suites import (
    DEFAULT_SUITE_NAME,
    RoundCase,
    RoundOracle,
    SUITES_VERSION,
    get_suite_description,
    list_suite_names,
    load_suite,
)
from scripts.test_multiround_10x10 import evaluate_round, extract_observed


@pytest.mark.unit
def test_multiround_suite_catalog_exposes_baseline_and_alternative() -> None:
    assert DEFAULT_SUITE_NAME == "baseline"
    assert SUITES_VERSION >= 3
    assert list_suite_names() == ["baseline", "alternative", "regression", "hardening"]
    assert "Alternative 10x10 benchmark" in get_suite_description("alternative")
    assert "StatsCan decomposition + timeframe retention" in get_suite_description("regression")
    assert "Stricter certification suite" in get_suite_description("hardening")


@pytest.mark.unit
@pytest.mark.parametrize("suite_name", ["baseline", "alternative"])
def test_each_multiround_suite_has_ten_named_tests_with_ten_oracle_rounds(suite_name: str) -> None:
    suite = load_suite(suite_name, now=datetime(2026, 4, 12, 12, 0, 0))
    assert len(suite) == 10

    for test_name, rounds in suite.items():
        assert test_name
        assert len(rounds) == 10
        for round_case in rounds:
            assert isinstance(round_case, RoundCase)
            assert round_case.query.strip()
            assert isinstance(round_case.oracle, RoundOracle)
            assert round_case.oracle.min_series_count >= 1
            assert (
                round_case.oracle.accepted_providers
                or round_case.oracle.required_countries
                or round_case.oracle.accepted_series_ids
                or round_case.oracle.required_indicator_cues
                or round_case.oracle.expect_clarification
            )


@pytest.mark.unit
def test_baseline_suite_preserves_existing_phase6_queries_with_oracles() -> None:
    suite = load_suite("baseline", now=datetime(2026, 4, 12, 12, 0, 0))

    assert [round_case.query for round_case in suite["Test 1: GDP Deep Dive"][:3]] == [
        "US GDP",
        "Add China GDP",
        "Add Germany GDP",
    ]
    first_statscan = suite["Test 4: Canada StatsCan Dimensions"][0]
    assert first_statscan.query == "Canada unemployment rate"
    assert first_statscan.oracle.accepted_providers == ("STATSCAN",)
    assert "unemployment" in first_statscan.oracle.required_indicator_cues


@pytest.mark.unit
def test_alternative_suite_targets_different_conversation_stress_patterns() -> None:
    suite = load_suite("alternative")

    assert [case.query for case in suite["Alt 4: StatsCan Province and Age"][:3]] == [
        "Canada employment rate",
        "Show by province",
        "Show only Ontario",
    ]
    assert [case.query for case in suite["Alt 9: Bilateral Trade Direction"][-2:]] == [
        "Switch to imports",
        "Show total trade Germany and United States",
    ]


@pytest.mark.unit
def test_regression_suite_covers_reported_statscan_sex_timeframe_bug() -> None:
    suite = load_suite("regression", now=datetime(2026, 4, 15, 12, 0, 0))

    assert [case.query for case in suite["Reg 1: StatsCan Sex Follow-up Horizon"]] == [
        "unemployment in Canada by sex",
        "last 20 years",
        "show only females",
    ]
    last_20_years_case = suite["Reg 1: StatsCan Sex Follow-up Horizon"][1]
    assert last_20_years_case.oracle.accepted_providers == ("STATSCAN",)
    assert last_20_years_case.oracle.accepted_frequencies == ("monthly",)
    assert last_20_years_case.oracle.min_points_per_series >= 200
    assert last_20_years_case.oracle.earliest_year_at_most == 2007
    assert last_20_years_case.oracle.latest_year_at_least == 2025
    females_case = suite["Reg 1: StatsCan Sex Follow-up Horizon"][2]
    assert females_case.oracle.forbidden_indicator_cues == ("male",)


@pytest.mark.unit
def test_regression_suite_covers_statscan_province_single_turn_horizon_bug() -> None:
    suite = load_suite("regression", now=datetime(2026, 4, 15, 12, 0, 0))

    province_case = suite["Reg 3: StatsCan Province Single-turn Horizon"][0]
    assert province_case.query == "give me unemployment by province in Canada in last 20 years"
    assert province_case.oracle.accepted_providers == ("STATSCAN",)
    assert province_case.oracle.accepted_frequencies == ("monthly",)
    assert province_case.oracle.exact_series_count == 10
    assert province_case.oracle.min_points_per_series >= 200
    assert province_case.oracle.earliest_year_at_most == 2007
    assert province_case.oracle.latest_year_at_least == 2025


@pytest.mark.unit
def test_oracle_evaluator_fails_data_presence_without_semantic_match() -> None:
    case = RoundCase(
        query="US GDP growth rate",
        oracle=RoundOracle(
            accepted_providers=("IMF",),
            required_countries=("US",),
            required_indicator_cues=("gdp", "growth"),
            exact_series_count=1,
        ),
    )
    resp_json = {
        "data": [
            {
                "metadata": {
                    "source": "World Bank",
                    "country": "United States",
                    "indicator": "GDP per capita (current US$)",
                    "seriesId": "NY.GDP.PCAP.CD",
                },
                "data": [{"date": "2024", "value": 1.0}],
            }
        ]
    }

    status, _, _, reasons, observed = evaluate_round(case, resp_json)
    assert status == "FAIL"
    assert any(reason.startswith("provider_mismatch") for reason in reasons)
    assert any(reason.startswith("missing_indicator_cue=growth") for reason in reasons)
    assert observed["series_count"] == 1


@pytest.mark.unit
def test_oracle_evaluator_accepts_expected_clarification_path() -> None:
    case = RoundCase(
        query="employment in Canada",
        oracle=RoundOracle(expect_clarification=True, required_option_cues=("employment rate", "number employed")),
    )
    resp_json = {
        "clarificationNeeded": True,
        "clarificationOptions": [
            {"label": "employment rate", "value": "employment rate in Canada"},
            {"label": "number employed", "value": "number employed in Canada"},
        ],
        "response": "Could you clarify which employment metric you want?",
    }

    status, _, _, reasons, observed = evaluate_round(case, resp_json)
    assert status == "PASS"
    assert reasons == []
    assert observed["clarification_detected"] is True


@pytest.mark.unit
def test_observed_semantic_tags_detect_inflation_and_growth_variants() -> None:
    inflation_response = {
        "data": [
            {
                "metadata": {
                    "source": "Eurostat",
                    "country": "Germany",
                    "indicator": "HICP - annual data (average index and rate of change)",
                    "seriesId": "PRC_HICP_AIND",
                },
                "data": [{"date": "2024", "value": 2.1}],
            }
        ]
    }
    growth_response = {
        "data": [
            {
                "metadata": {
                    "source": "FRED",
                    "country": "United States",
                    "indicator": "Real gross domestic product",
                    "seriesId": "A191RL1Q225SBEA",
                },
                "data": [{"date": "2024", "value": 2.2}],
            }
        ]
    }

    inflation_observed = extract_observed(inflation_response)
    growth_observed = extract_observed(growth_response)

    assert "inflation" in inflation_observed["semantic_tags"]
    assert "hicp" in inflation_observed["semantic_tags"]
    assert "growth" in growth_observed["semantic_tags"]
    assert "gdp" in growth_observed["semantic_tags"]


@pytest.mark.unit
def test_country_normalization_handles_world_bank_country_labels() -> None:
    case = RoundCase(
        query="Add South Korea exports share of GDP",
        oracle=RoundOracle(
            required_countries=("JP", "KR"),
            required_indicator_cues=("export", "gdp"),
            exact_series_count=2,
        ),
    )
    resp_json = {
        "data": [
            {
                "metadata": {
                    "source": "World Bank",
                    "country": "Japan",
                    "indicator": "Exports of goods and services (% of GDP)",
                    "seriesId": "NE.EXP.GNFS.ZS",
                },
                "data": [{"date": "2024", "value": 1.0}],
            },
            {
                "metadata": {
                    "source": "World Bank",
                    "country": "Korea, Rep.",
                    "indicator": "Exports of goods and services (% of GDP)",
                    "seriesId": "NE.EXP.GNFS.ZS",
                },
                "data": [{"date": "2024", "value": 1.1}],
            },
        ]
    }

    status, _, _, reasons, observed = evaluate_round(case, resp_json)
    assert status == "PASS"
    assert reasons == []
    assert observed["countries"] == ["JP", "KR"]


@pytest.mark.unit
def test_oracle_evaluator_checks_frequency_point_count_and_year_span() -> None:
    case = RoundCase(
        query="last 20 years",
        oracle=RoundOracle(
            accepted_providers=("STATSCAN",),
            required_countries=("CA",),
            required_indicator_cues=("unemployment", "male", "female"),
            accepted_frequencies=("monthly",),
            exact_series_count=2,
            min_points_per_series=200,
            earliest_year_at_most=2007,
            latest_year_at_least=2025,
        ),
    )
    resp_json = {
        "data": [
            {
                "metadata": {
                    "source": "Statistics Canada",
                    "country": "Canada",
                    "indicator": "male unemployment rate",
                    "frequency": "monthly",
                    "seriesId": "14100287:male",
                },
                "data": [{"date": f"{year}-01-01", "value": 1.0} for year in range(2015, 2025)],
            },
            {
                "metadata": {
                    "source": "Statistics Canada",
                    "country": "Canada",
                    "indicator": "female unemployment rate",
                    "frequency": "monthly",
                    "seriesId": "14100287:female",
                },
                "data": [{"date": f"{year}-01-01", "value": 1.0} for year in range(2015, 2025)],
            },
        ]
    }

    status, _, _, reasons, observed = evaluate_round(case, resp_json)
    assert status == "FAIL"
    assert "points_per_series<200" in reasons
    assert "earliest_year>2007" in reasons
    assert observed["frequencies"] == ["monthly"]
