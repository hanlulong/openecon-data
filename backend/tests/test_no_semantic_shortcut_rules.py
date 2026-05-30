from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.models import ExecutionPlan, ParsedIntent
from backend.providers.imf import IMFProvider
from backend.routing.unified_router import UnifiedRouter
from backend.services.data_fetcher import fetch_from_provider_dispatch
from backend.services.indicator_resolution import (
    apply_catalog_availability_override,
    apply_concept_provider_override,
    resolve_indicator_for_fetch,
)
from backend.services.query import QueryService
from backend.tests.semantic_shortcut_audit import (
    CURRENT_BANNED_SEMANTIC_DEBT,
    iter_scan_paths,
    scan_semantic_shortcuts,
)
from backend.utils.retry import DataNotAvailableError


RUNTIME_FILES = [
    Path("backend/services/indicator_resolution.py"),
    Path("backend/services/query.py"),
    Path("backend/services/data_fetcher.py"),
    Path("backend/routing/unified_router.py"),
    Path("backend/services/indicator_selector.py"),
    Path("backend/services/indicator_clarification.py"),
    Path("backend/services/query_helpers.py"),
    Path("backend/services/provider_fallback.py"),
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


def test_expanded_semantic_shortcut_scan_scope_covers_plan_required_files() -> None:
    scanned = {path.as_posix() for path in iter_scan_paths()}

    required = {
        "backend/routing/unified_router.py",
        "backend/services/indicator_resolution.py",
        "backend/services/indicator_selector.py",
        "backend/services/indicator_clarification.py",
        "backend/services/query.py",
        "backend/services/query_helpers.py",
        "backend/services/provider_fallback.py",
        "backend/services/query_parsing.py",
        "backend/services/relevance_scorer.py",
        "backend/services/statscan_metadata.py",
        "backend/providers/statscan.py",
        "backend/providers/oecd.py",
        "backend/providers/eurostat.py",
        "backend/providers/imf.py",
        "backend/providers/bis.py",
        "backend/providers/fred.py",
        "backend/utils/imf_supportability.py",
    }

    assert required <= scanned


def test_semantic_shortcut_audit_classifies_current_rule_surfaces() -> None:
    findings = scan_semantic_shortcuts()

    assert findings, "expanded scanner should report the reviewed Phase 0 rule surfaces"
    assert all(finding.classification for finding in findings)
    assert all(finding.rationale for finding in findings)

    found_ids = {finding.pattern_id for finding in findings}
    assert {
        "unified_router_provider_candidate_metadata",
        "indicator_resolution_exact_code_literal_gate",
        "indicator_resolution_fail_closed_plausibility_guard",
        "query_parsing_cue_inference",
    } <= found_ids



def test_imf_supportability_does_not_use_query_text_marker_sets() -> None:
    source = Path("backend/utils/imf_supportability.py").read_text()

    forbidden_markers = {
        "_DETAIL_MARKERS",
        "_CONSUMER_PRICE_DETAIL_MARKERS",
        "_FISCAL_DETAIL_MARKERS",
        "_NATIONAL_ACCOUNTS_DETAIL_MARKERS",
        "_SOCIAL_DEMOGRAPHIC_DETAIL_MARKERS",
        "_COMPLEX_FINANCE_DETAIL_MARKERS",
        "_SPECIAL_PUBLIC_ENTITY_MARKERS",
    }

    assert [marker for marker in forbidden_markers if marker in source] == []


def test_imf_bop_label_matching_is_not_runtime_final_authority() -> None:
    source = inspect.getsource(IMFProvider.fetch_batch_indicator)

    assert "_fetch_bop_family(" not in source
    assert "_likely_dataset_family_hint(" not in source
    assert "no-rule authority policy" in source


def test_imf_sdmx_exact_candidate_helpers_do_not_use_label_semantics() -> None:
    """Public-SDMX exact candidate construction must stay code-mechanical."""

    trade_indicator = inspect.getsource(IMFProvider._trade_indicator_from_code)
    trade_transformation = inspect.getsource(IMFProvider._trade_transformation_from_code)
    cpi_coicop = inspect.getsource(IMFProvider._coicop_from_cpi_code)
    ppi_indicator = inspect.getsource(IMFProvider._ppi_indicator_from_code)

    combined = "\n".join([trade_indicator, trade_transformation, cpi_coicop, ppi_indicator])

    assert "label" not in combined.lower()
    assert "food" not in combined.lower()
    assert "export" not in combined.lower()
    assert "import" not in combined.lower()
    assert "producer price" not in combined.lower()

def test_indicator_resolution_no_longer_promotes_rule_plausibility_to_authority() -> None:
    """Rule plausibility guards may fail closed, but must not create final authority."""

    source = inspect.getsource(resolve_indicator_for_fetch)

    assert "implausible_llm_pick" not in source
    assert "is_resolved_indicator_plausible(" not in source
    assert "refusing deterministic plausibility promotion" in source


def test_legacy_resolver_and_translator_modules_are_removed() -> None:
    retired_paths = [
        Path("backend/services/indicator_resolver.py"),
        Path("backend/services/indicator_translator.py"),
    ]

    assert [path.as_posix() for path in retired_paths if path.exists()] == []


def test_runtime_code_does_not_import_retired_resolver_or_translator() -> None:
    forbidden = (
        "indicator_resolver",
        "indicator_translator",
        "IndicatorResolver",
        "IndicatorTranslator",
        "get_indicator_resolver",
        "get_indicator_translator",
        "translate_indicator",
    )
    scanned = [
        *Path("backend/services").glob("*.py"),
        *Path("backend/providers").glob("*.py"),
        *Path("backend/routing").glob("*.py"),
    ]

    offenders: list[str] = []
    for path in scanned:
        if path.name in {"semantic_shortcut_audit.py"}:
            continue
        text = path.read_text()
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{path}:{marker}")

    assert offenders == []


def test_tests_do_not_patch_retired_resolver_shims() -> None:
    """Avoid no-op create=True patches for deleted shortcut modules in tests."""

    forbidden = (
        "backend.services.query.get_indicator_" + "resolver",
        "backend.services.indicator_" + "resolver.get_indicator_resolver",
        "backend.services.indicator_" + "translator.get_indicator_translator",
    )
    scanned = [
        Path("backend/tests/test_query_service.py"),
        Path("backend/tests/test_providers.py"),
        Path("backend/tests/test_indicator_resolution.py"),
    ]

    offenders = [
        f"{path}:{marker}"
        for path in scanned
        for marker in forbidden
        if marker in path.read_text()
    ]

    assert offenders == []


def test_no_new_banned_semantic_final_authority_findings_outside_debt_ledger() -> None:
    findings = scan_semantic_shortcuts()

    untracked = [
        f"{finding.path}:{finding.pattern_id}:{finding.line_number}"
        for finding in findings
        if finding.classification == "banned_semantic_final_authority"
        and finding.review_key not in CURRENT_BANNED_SEMANTIC_DEBT
    ]

    assert untracked == []


def test_current_banned_semantic_debt_ledger_matches_scan() -> None:
    findings = scan_semantic_shortcuts()

    actual_debt_keys = {
        finding.review_key
        for finding in findings
        if finding.classification == "banned_semantic_final_authority"
    }

    assert actual_debt_keys == CURRENT_BANNED_SEMANTIC_DEBT


def test_final_completion_requires_empty_banned_semantic_debt_ledger() -> None:
    """Final gate: no banned semantic final-authority findings remain."""

    findings = scan_semantic_shortcuts()

    banned = [
        f"{finding.path}:{finding.pattern_id}:{finding.line_number}:{finding.line}"
        for finding in findings
        if finding.classification == "banned_semantic_final_authority"
    ]

    assert CURRENT_BANNED_SEMANTIC_DEBT == frozenset()
    assert banned == []


def test_unified_router_has_no_banned_provider_final_authority_findings() -> None:
    findings = scan_semantic_shortcuts()

    unified_banned = [
        f"{finding.path}:{finding.pattern_id}:{finding.line_number}:{finding.line}"
        for finding in findings
        if finding.path.as_posix() == "backend/routing/unified_router.py"
        and finding.classification == "banned_semantic_final_authority"
    ]

    assert unified_banned == []


def test_indicator_clarification_has_no_legacy_resolver_option_authority() -> None:
    findings = scan_semantic_shortcuts()

    clarification_banned = [
        f"{finding.path}:{finding.pattern_id}:{finding.line_number}:{finding.line}"
        for finding in findings
        if finding.path.as_posix() == "backend/services/indicator_clarification.py"
        and finding.pattern_id == "indicator_clarification_legacy_resolver_options"
    ]

    assert clarification_banned == []


def test_provider_fallback_has_no_legacy_resolver_or_catalog_provider_choice() -> None:
    findings = scan_semantic_shortcuts()

    fallback_banned = [
        f"{finding.path}:{finding.pattern_id}:{finding.line_number}:{finding.line}"
        for finding in findings
        if finding.path.as_posix() == "backend/services/provider_fallback.py"
        and finding.pattern_id == "provider_fallback_legacy_semantic_provider_choice"
    ]

    assert fallback_banned == []


def test_indicator_selector_has_no_top_candidate_final_authority_fallback() -> None:
    findings = scan_semantic_shortcuts()

    selector_banned = [
        f"{finding.path}:{finding.pattern_id}:{finding.line_number}:{finding.line}"
        for finding in findings
        if finding.path.as_posix() == "backend/services/indicator_selector.py"
        and finding.pattern_id == "indicator_selector_top_candidate_fallback"
    ]

    assert selector_banned == []


def test_optional_router_modes_have_no_legacy_disabled_bypass_findings() -> None:
    findings = scan_semantic_shortcuts()

    legacy_disabled = [
        f"{finding.path}:{finding.pattern_id}:{finding.line_number}:{finding.line}"
        for finding in findings
        if finding.classification == "legacy_disabled_must_not_enable"
    ]

    assert legacy_disabled == []


def test_default_path_blocks_provider_internal_map_dispatch_without_authority() -> None:
    svc = SimpleNamespace(
        settings=SimpleNamespace(),
        fred_provider=SimpleNamespace(fetch_series=lambda _params: None),
    )
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["mortgage rate"],
        parameters={"indicator": "mortgage rate"},
        clarificationNeeded=False,
        originalQuery="mortgage rate in the US",
    )
    plan = ExecutionPlan(
        provider="FRED",
        candidate_id="FRED:MORTGAGE_RATE",
        fetch_strategy="provider_dispatch",
        params={"indicator": "mortgage rate"},
        provider_request={"series_id": "mortgage rate"},
    )

    with pytest.raises(DataNotAvailableError, match="blocked provider-internal map dispatch"):
        asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))


def test_provider_internal_map_dispatch_has_no_settings_escape_hatch() -> None:
    svc = SimpleNamespace(
        settings=SimpleNamespace(allow_legacy_provider_map_final_authority=True),
        fred_provider=SimpleNamespace(fetch_series=lambda _params: None),
    )
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["mortgage rate"],
        parameters={"indicator": "mortgage rate"},
        clarificationNeeded=False,
        originalQuery="mortgage rate in the US",
    )
    plan = ExecutionPlan(
        provider="FRED",
        candidate_id="FRED:MORTGAGE_RATE",
        fetch_strategy="provider_dispatch",
        params={"indicator": "mortgage rate"},
        provider_request={"series_id": "mortgage rate"},
    )

    with pytest.raises(DataNotAvailableError, match="blocked provider-internal map dispatch"):
        asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))


def test_promoted_path_allows_exact_provider_code_dispatch_as_mechanical_authority() -> None:
    async def _fetch_series(params: dict) -> dict:
        return {"indicator": params["indicator"]}

    svc = SimpleNamespace(
        settings=SimpleNamespace(
            use_outcome_decision_stage=True,
            use_post_fetch_semantic_judge=True,
            use_staged_state_commit=True,
        ),
        fred_provider=SimpleNamespace(fetch_series=_fetch_series),
    )
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["MORTGAGE30US"],
        parameters={
            "indicator": "MORTGAGE30US",
            "__exact_provider_code_match": True,
            "__semantic_authority": "exact_user_input",
            "__decision_source": "exact_code",
        },
        clarificationNeeded=False,
        originalQuery="FRED MORTGAGE30US",
    )
    plan = ExecutionPlan(
        provider="FRED",
        candidate_id="FRED:MORTGAGE30US",
        fetch_strategy="provider_dispatch",
        params=dict(intent.parameters),
        provider_request={"series_id": "MORTGAGE30US"},
    )

    result = asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))

    assert result == [{"indicator": "MORTGAGE30US"}]


def test_promoted_path_allows_llm_adjudicated_provider_code_dispatch() -> None:
    async def _fetch_series(params: dict) -> dict:
        return {"indicator": params["indicator"]}

    svc = SimpleNamespace(
        settings=SimpleNamespace(
            use_outcome_decision_stage=True,
            use_post_fetch_semantic_judge=True,
            use_staged_state_commit=True,
        ),
        fred_provider=SimpleNamespace(fetch_series=_fetch_series),
    )
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["mortgage rate"],
        parameters={
            "indicator": "MORTGAGE30US",
            "__semantic_authority": "llm_adjudication",
            "__decision_source": "llm_pick",
        },
        clarificationNeeded=False,
        originalQuery="mortgage rate in the US",
    )
    plan = ExecutionPlan(
        provider="FRED",
        candidate_id="FRED:MORTGAGE30US",
        fetch_strategy="provider_dispatch",
        params=dict(intent.parameters),
        provider_request={"series_id": "MORTGAGE30US"},
    )

    result = asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))

    assert result == [{"indicator": "MORTGAGE30US"}]
