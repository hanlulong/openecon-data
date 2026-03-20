from __future__ import annotations

from backend.services.parameter_mapper import ParameterMapper


def test_normalize_indicator_does_not_fuzzy_match_employment_to_unemployment():
    mapper = ParameterMapper()

    assert mapper.normalize_indicator("STATSCAN", "employment rate") is None
    assert mapper.normalize_indicator("WORLDBANK", "employment rate") is None


def test_normalize_indicator_keeps_valid_unemployment_match():
    mapper = ParameterMapper()

    assert mapper.normalize_indicator("STATSCAN", "unemployment rate") == "2062815"
