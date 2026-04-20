from __future__ import annotations

from unittest.mock import Mock, patch

from backend.services.indicator_resolution import (
    build_exact_indicator_title_intent,
    is_exact_match_locked,
    is_provider_locked,
)


def test_build_exact_indicator_title_intent_builds_provider_locked_intent() -> None:
    lookup_results = [
        {
            "provider": "FRED",
            "code": "MELIPRVSUSCOUNTY24005",
            "name": "Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
        }
    ]

    with patch(
        "backend.services.indicator_database.get_indicator_lookup",
        return_value=Mock(search=Mock(return_value=lookup_results)),
    ):
        intent = build_exact_indicator_title_intent(
            "US Market Hotness: Median Listing Price Versus the United States in Baltimore County, MD",
            explicit_provider="FRED",
            countries=["US"],
            all_providers=["FRED"],
        )

    assert intent is not None
    assert intent.apiProvider == "FRED"
    assert intent.parameters["indicator"] == "MELIPRVSUSCOUNTY24005"
    assert intent.parameters["country"] == "US"
    assert intent.parameters["__semantic_provider_locked"] is True
    assert intent.parameters["__exact_indicator_title_match"] is True


def test_build_exact_indicator_title_intent_rejects_generic_suffix_only_match() -> None:
    class _Lookup:
        def search(self, text, provider=None, limit=5):
            if provider == "FRED":
                return [
                    {
                        "provider": "FRED",
                        "code": "PCETRIM12M159SFRBDAL",
                        "name": "Trimmed Mean PCE Inflation Rate",
                    }
                ]
            return []

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=_Lookup()):
        intent = build_exact_indicator_title_intent(
            "Germany inflation rate",
            all_providers=["FRED"],
        )

    assert intent is None


def test_build_exact_indicator_title_intent_preserves_broad_catalog_concept() -> None:
    lookup_results = [
        {
            "provider": "FRED",
            "code": "REAINTRATREARAT10Y",
            "name": "10-Year Real Interest Rate",
        }
    ]

    with patch(
        "backend.services.indicator_database.get_indicator_lookup",
        return_value=Mock(search=Mock(return_value=lookup_results)),
    ):
        intent = build_exact_indicator_title_intent(
            "interest rate",
            broad_concept="interest rate",
            all_providers=["FRED"],
        )

    assert intent is not None
    assert intent.parameters["__catalog_concept"] == "interest rate"
    assert intent.parameters["__exact_indicator_title_match"] is True


def test_exact_and_provider_lock_helpers_read_shared_flags() -> None:
    params = {
        "__semantic_provider_locked": True,
        "__exact_indicator_title_match": True,
    }

    assert is_provider_locked(params) is True
    assert is_exact_match_locked(params) is True
    assert is_provider_locked({}) is False
    assert is_exact_match_locked({}) is False
