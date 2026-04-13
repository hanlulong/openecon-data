from __future__ import annotations

from datetime import datetime

import pytest

from scripts.multiround_suites import (
    DEFAULT_SUITE_NAME,
    get_suite_description,
    list_suite_names,
    load_suite,
)


@pytest.mark.unit
def test_multiround_suite_catalog_exposes_baseline_and_alternative() -> None:
    assert DEFAULT_SUITE_NAME == "baseline"
    assert list_suite_names() == ["baseline", "alternative"]
    assert "Alternative 10x10 benchmark" in get_suite_description("alternative")


@pytest.mark.unit
@pytest.mark.parametrize("suite_name", ["baseline", "alternative"])
def test_each_multiround_suite_has_ten_named_tests_with_ten_rounds(suite_name: str) -> None:
    suite = load_suite(suite_name, now=datetime(2026, 4, 12, 12, 0, 0))
    assert len(suite) == 10

    for test_name, rounds in suite.items():
        assert test_name
        assert len(rounds) == 10
        assert all(isinstance(query, str) and query.strip() for query in rounds)


@pytest.mark.unit
def test_baseline_suite_preserves_existing_phase6_queries() -> None:
    suite = load_suite("baseline", now=datetime(2026, 4, 12, 12, 0, 0))

    assert suite["Test 1: GDP Deep Dive"][:3] == [
        "US GDP",
        "Add China GDP",
        "Add Germany GDP",
    ]
    assert suite["Test 4: Canada StatsCan Dimensions"][0] == "Canada unemployment rate"


@pytest.mark.unit
def test_alternative_suite_targets_different_conversation_stress_patterns() -> None:
    suite = load_suite("alternative")

    assert suite["Alt 4: StatsCan Province and Age"][:3] == [
        "Canada employment rate",
        "Show by province",
        "Show only Ontario",
    ]
    assert suite["Alt 9: Bilateral Trade Direction"][-2:] == [
        "Switch to imports",
        "Show total trade Germany and United States",
    ]
