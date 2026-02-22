from __future__ import annotations

from unittest.mock import patch

from backend.models import ParsedIntent
from backend.services.query_pipeline import QueryPipeline
from backend.tests.utils import run


class _OpenRouterStub:
    def __init__(self, intent: ParsedIntent):
        self._intent = intent

    async def parse_query(self, query: str, history=None) -> ParsedIntent:
        return self._intent.model_copy(deep=True)


class _ServiceStub:
    def __init__(self, intent: ParsedIntent):
        self.openrouter = _OpenRouterStub(intent)
        self.country_overrides_applied = False
        self.detected_provider = None
        self.routed_provider = intent.apiProvider

    def _apply_country_overrides(self, intent: ParsedIntent, query: str) -> None:
        self.country_overrides_applied = True

    def _detect_explicit_provider(self, query: str):
        return self.detected_provider

    def _normalize_provider_alias(self, provider: str):
        return provider

    async def _select_routed_provider(self, intent: ParsedIntent, query: str) -> str:
        return self.routed_provider



def _intent() -> ParsedIntent:
    return ParsedIntent(
        apiProvider="WorldBank",
        indicators=["GDP"],
        parameters={"country": "US"},
        clarificationNeeded=False,
    )


def test_parse_and_route_enforces_explicit_provider() -> None:
    service = _ServiceStub(_intent())
    service.detected_provider = "OECD"
    service.routed_provider = "IMF"  # Should be ignored when explicit provider exists.
    pipeline = QueryPipeline(service)

    with patch("backend.services.query_pipeline.ProviderRouter.validate_routing", return_value=None):
        result = run(pipeline.parse_and_route("gdp from oecd", history=[]))

    assert service.country_overrides_applied is True
    assert result.explicit_provider == "OECD"
    assert result.routed_provider == "OECD"
    assert result.intent.apiProvider == "OECD"


def test_parse_and_route_uses_router_when_no_explicit_provider() -> None:
    service = _ServiceStub(_intent())
    service.detected_provider = None
    service.routed_provider = "IMF"
    pipeline = QueryPipeline(service)

    with patch("backend.services.query_pipeline.ProviderRouter.validate_routing", return_value="warn"):
        result = run(pipeline.parse_and_route("gdp in us", history=[]))

    assert result.explicit_provider is None
    assert result.routed_provider == "IMF"
    assert result.intent.apiProvider == "IMF"
    assert result.validation_warning == "warn"


def test_validate_intent_multi_indicator_short_circuit() -> None:
    intent = ParsedIntent(
        apiProvider="WorldBank",
        indicators=["GDP", "CPI"],
        parameters={"country": "US"},
        clarificationNeeded=False,
    )
    service = _ServiceStub(intent)
    pipeline = QueryPipeline(service)

    result = pipeline.validate_intent(intent)
    assert result.is_multi_indicator is True
    assert result.is_valid is True
    assert result.is_confident is True


def test_validate_intent_invalid() -> None:
    intent = _intent()
    service = _ServiceStub(intent)
    pipeline = QueryPipeline(service)

    with patch("backend.services.query_pipeline.ParameterValidator.validate_intent", return_value=(False, "bad", {"example": "x"})):
        result = pipeline.validate_intent(intent)

    assert result.is_valid is False
    assert result.validation_error == "bad"
    assert result.is_confident is False
