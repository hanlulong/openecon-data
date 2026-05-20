from __future__ import annotations

from backend.models import ParsedIntent
from backend.services.parameter_validator import ParameterValidator


def _worldbank_intent(parameters: dict) -> ParsedIntent:
    return ParsedIntent(
        apiProvider="WORLDBANK",
        indicators=["Teachers in post-secondary non-tertiary education, female (number)"],
        parameters=parameters,
        clarificationNeeded=False,
        confidence=0.99,
        recommendedChartType="line",
        queryType="data_fetch",
        originalQuery="Are Teachers in post-secondary non-tertiary education female (number) from World Bank",
        isFollowUp=False,
        followUpType=None,
        resolvedQuery=None,
        needsDecomposition=False,
        decompositionType=None,
        decompositionEntities=None,
        useProMode=False,
    )


def test_worldbank_exact_title_without_country_defaults_to_all() -> None:
    intent = _worldbank_intent(
        {
            "indicator": "UIS.T.4.F",
            "__exact_indicator_title_match": True,
            "__semantic_provider_locked": True,
        }
    )

    valid, error, suggestions = ParameterValidator.validate_intent(intent)

    assert valid is True
    assert error is None
    assert intent.parameters["country"] == "all"
    assert intent.parameters["__worldbank_defaulted_country_all"] is True
    assert suggestions and "Defaulted exact WorldBank" in suggestions["note"]


def test_worldbank_non_exact_query_without_country_still_requires_scope() -> None:
    intent = _worldbank_intent({"indicator": "UIS.T.4.F"})

    valid, error, suggestions = ParameterValidator.validate_intent(intent)

    assert valid is False
    assert error == "World Bank query requires a country or list of countries"
    assert suggestions and "Specify which country" in suggestions["suggestion"]
