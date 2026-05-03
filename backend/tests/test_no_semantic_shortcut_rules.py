from __future__ import annotations

import inspect
from pathlib import Path

from backend.models import ParsedIntent
from backend.routing.unified_router import UnifiedRouter
from backend.services.indicator_resolution import (
    apply_catalog_availability_override,
    apply_concept_provider_override,
)
from backend.services.query import QueryService


RUNTIME_FILES = [
    Path("backend/services/indicator_resolution.py"),
    Path("backend/services/query.py"),
    Path("backend/services/data_fetcher.py"),
    Path("backend/routing/unified_router.py"),
    Path("backend/routing/hybrid_router.py"),
    Path("backend/services/indicator_selector.py"),
    Path("backend/services/indicator_clarification.py"),
    Path("backend/services/query_helpers.py"),
    Path("backend/services/query_pipeline.py"),
]

FORBIDDEN_RUNTIME_MARKERS = [
    "Concept override:",
    "Concept code override:",
    "Concept override indicator:",
    "Catalog concept override locked",
    "Catalog remapped indicator",
    "Catalog availability override",
    "catalog recommended",
    "__catalog_resolved",
    "__catalog_concept",
    "_AMBIGUOUS_CONCEPT_OPTIONS",
    "indicator_translator.translate_indicator",
    "_resolve_imf_aggregate_indicator_fast_path",
    "__imf_public_sdmx_fast_path",
    "_route_by_catalog",
    "Catalog lookup:",
    "Catalog match:",
    'match_type="catalog"',
]


def test_concept_provider_override_is_identity_even_when_catalog_would_reroute() -> None:
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    intent = ParsedIntent(
        apiProvider="STATSCAN",
        indicators=["private households by size"],
        parameters={"country": "CA", "indicator": "17100159"},
        clarificationNeeded=False,
        originalQuery="private households by size in Canada in 2021",
    )
    params = dict(intent.parameters or {})

    provider, new_params = apply_concept_provider_override(svc, "STATSCAN", intent, params)

    assert provider == "STATSCAN"
    assert new_params == params
    assert intent.apiProvider == "STATSCAN"
    assert intent.indicators == ["private households by size"]


def test_catalog_availability_override_is_identity() -> None:
    svc = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
    intent = ParsedIntent(
        apiProvider="STATSCAN",
        indicators=["WS_TC"],
        parameters={"country": "CA", "indicator": "WS_TC"},
        clarificationNeeded=False,
        originalQuery="total private households in Canada in 2025",
    )
    params = dict(intent.parameters or {})

    provider, new_params = apply_catalog_availability_override(
        svc,
        "STATSCAN",
        intent,
        params,
        fallback_excluded_providers=set(),
    )

    assert provider == "STATSCAN"
    assert new_params == params
    assert intent.apiProvider == "STATSCAN"


def test_provider_selection_does_not_call_semantic_concept_override() -> None:
    source = inspect.getsource(QueryService._select_routed_provider)

    assert "_apply_concept_provider_override" not in source
    assert "_apply_catalog_availability_override" not in source


def test_unified_router_does_not_use_catalog_semantic_routing() -> None:
    class ExplodingCatalog:
        def find_concept_by_term(self, *_args, **_kwargs):  # noqa: ANN001
            raise AssertionError("router must not ask catalog for semantic provider routing")

    router = UnifiedRouter(catalog_service=ExplodingCatalog(), use_catalog=True)

    decision = router.route("GDP", indicators=["GDP"])

    assert decision.provider.upper() == "WORLDBANK"
    assert decision.match_type != "catalog"


def test_runtime_matching_files_do_not_contain_forced_catalog_or_translation_markers() -> None:
    offenders: list[str] = []
    for path in RUNTIME_FILES:
        text = path.read_text()
        for marker in FORBIDDEN_RUNTIME_MARKERS:
            if marker in text:
                offenders.append(f"{path}:{marker}")

    assert offenders == []
