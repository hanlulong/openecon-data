"""Static audit helpers for semantic shortcut rule surfaces.

The scanner is deliberately conservative: it tracks known high-risk
semantic-authority surfaces and requires every match to be classified.  Deleted
legacy resolver/translator modules are intentionally absent from the scan scope;
tests assert that no runtime code imports those retired surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal


ShortcutClassification = Literal[
    "allowed_mechanical",
    "candidate_generation_only",
    "fail_closed_supportability",
    "banned_semantic_final_authority",
    "legacy_disabled_must_not_enable",
]


@dataclass(frozen=True)
class ShortcutPattern:
    """A reviewed static pattern for rule-surface classification."""

    id: str
    path_glob: str
    regex: str
    classification: ShortcutClassification
    rationale: str


@dataclass(frozen=True)
class ShortcutFinding:
    """One static rule-surface finding."""

    pattern_id: str
    path: Path
    line_number: int
    line: str
    classification: ShortcutClassification
    rationale: str

    @property
    def review_key(self) -> tuple[str, str]:
        return (self.path.as_posix(), self.pattern_id)


SCAN_GLOBS = (
    "backend/routing/*.py",
    "backend/services/indicator_resolution.py",
    "backend/services/indicator_selector.py",
    "backend/services/indicator_clarification.py",
    "backend/services/query.py",
    "backend/services/query_helpers.py",
    "backend/services/provider_fallback.py",
    "backend/services/query_parsing.py",
    "backend/services/relevance_scorer.py",
    "backend/services/statscan_metadata.py",
    "backend/providers/*.py",
    "backend/utils/imf_supportability.py",
)


PATTERNS = (
    ShortcutPattern(
        id="unified_router_provider_candidate_metadata",
        path_glob="backend/routing/unified_router.py",
        regex=r"_coverage_candidates|_correct_coingecko",
        classification="candidate_generation_only",
        rationale="UnifiedRouter may expose coverage candidates or reject impossible providers, but must not use them as final semantic authority.",
    ),
    ShortcutPattern(
        id="unified_router_removed_semantic_provider_authority",
        path_glob="backend/routing/unified_router.py",
        regex=r"_is_(?:property_market|imf_macro|non_bilateral_trade_flow)_query|_route_by_(?:regional_group|country)|_handle_canadian_query",
        classification="banned_semantic_final_authority",
        rationale="Broad topic/country provider routing must not be reintroduced as final authority.",
    ),
    ShortcutPattern(
        id="indicator_resolution_legacy_fallback",
        path_glob="backend/services/indicator_resolution.py",
        regex=r"Legacy IndicatorResolver|_resolver\(\)\.resolve|fall through to legacy resolver",
        classification="banned_semantic_final_authority",
        rationale="Indicator resolution still falls through to legacy resolver after selector failure.",
    ),
    ShortcutPattern(
        id="indicator_resolution_exact_code_literal_gate",
        path_glob="backend/services/indicator_resolution.py",
        regex=r"_exact_provider_code_literal_present|literal user-supplied .* indicator code",
        classification="allowed_mechanical",
        rationale="Literal provider-code passthrough is mechanical exact user input, not semantic inference.",
    ),
    ShortcutPattern(
        id="indicator_resolution_fail_closed_plausibility_guard",
        path_glob="backend/services/indicator_resolution.py",
        regex=r"def is_resolved_indicator_plausible|def has_implausible_top_series|return not is_resolved_indicator_plausible|fail-closed guardrails",
        classification="fail_closed_supportability",
        rationale="Deterministic plausibility logic may only block/retry unsupported or implausible outputs; it must not select or promote final semantic authority.",
    ),
    ShortcutPattern(
        id="indicator_resolution_candidate_code_hints",
        path_glob="backend/services/indicator_resolution.py",
        regex=r"def code_semantic_hint|code_semantic_hint\(",
        classification="candidate_generation_only",
        rationale="Provider-code hints may enrich candidate scoring text but must not become a standalone concept-to-code shortcut.",
    ),
    ShortcutPattern(
        id="indicator_resolution_fail_closed_thresholds",
        path_glob="backend/services/indicator_resolution.py",
        regex=r"def minimum_resolved_relevance_threshold|def indicator_resolution_threshold|strict_precision_cues|high_precision_cues",
        classification="fail_closed_supportability",
        rationale="Static threshold logic may fail closed on weak evidence; it must not replace selector or LLM adjudication as semantic authority.",
    ),
    ShortcutPattern(
        id="indicator_clarification_legacy_resolver_options",
        path_glob="backend/services/indicator_clarification.py",
        regex=r"get_indicator_resolver|IndicatorResolver|indicator_resolver|resolver\.resolve",
        classification="banned_semantic_final_authority",
        rationale="Clarification/recovery options must come from selector candidate evidence, not legacy resolver/catalog authority.",
    ),
    ShortcutPattern(
        id="indicator_selector_top_candidate_fallback",
        path_glob="backend/services/indicator_selector.py",
        regex=r"top_candidate|fallback_code",
        classification="banned_semantic_final_authority",
        rationale="Selector can return top retrieval candidate when LLM cannot decide.",
    ),
    ShortcutPattern(
        id="query_unified_router_override",
        path_glob="backend/services/query.py",
        regex=r"UnifiedRouter override|intent\.apiProvider = routed|_build_explicit_provider_code_intent|_build_exact_indicator_title_intent",
        classification="candidate_generation_only",
        rationale="QueryService includes exact passthrough plus router override paths; later phases must separate mechanical from semantic authority.",
    ),
    ShortcutPattern(
        id="query_helpers_catalog_fallback",
        path_glob="backend/services/query_helpers.py",
        regex=r"resolve_concept_for_fallback|resolve_indicator_for_fallback_provider|get_best_provider|find_concept_by_term",
        classification="banned_semantic_final_authority",
        rationale="Cross-provider fallback can resolve concepts through catalog code mappings.",
    ),
    ShortcutPattern(
        id="provider_fallback_legacy_semantic_provider_choice",
        path_glob="backend/services/provider_fallback.py",
        regex=r"IndicatorResolver|indicator_resolver|get_indicator_resolver|catalog-based|get_compat_fallbacks|catalog_service",
        classification="banned_semantic_final_authority",
        rationale="Fallback provider ordering must not use legacy resolver/catalog semantic matches as final provider choice.",
    ),
    ShortcutPattern(
        id="query_parsing_cue_inference",
        path_glob="backend/services/query_parsing.py",
        regex=r"infer_multi_concept_indicators_from_query|extract_indicator_cues|inferred\.append",
        classification="candidate_generation_only",
        rationale="Cue-based parsing may generate candidate indicators but must not become final authority.",
    ),
    ShortcutPattern(
        id="relevance_scorer_cue_map",
        path_glob="backend/services/relevance_scorer.py",
        regex=r"_CUE_MAP|rerank_data_by_query_relevance|score_series_relevance",
        classification="candidate_generation_only",
        rationale="Cue-map relevance can rank evidence but must not replace semantic adjudication.",
    ),
    ShortcutPattern(
        id="statscan_semantic_product_maps",
        path_glob="backend/providers/statscan.py",
        regex=r"KEYWORD_SYNONYMS|VECTOR_MAPPINGS|COORDINATE_PRODUCT_MAPPINGS|MEMBER_KEYWORD_ALIASES|Using hardcoded mapping",
        classification="banned_semantic_final_authority",
        rationale="StatsCan maps can choose products/vectors from natural-language concepts.",
    ),
    ShortcutPattern(
        id="statscan_known_products",
        path_glob="backend/services/statscan_metadata.py",
        regex=r"KNOWN_PRODUCTS|Using known product",
        classification="banned_semantic_final_authority",
        rationale="StatsCan known products can bypass metadata search/adjudication.",
    ),
    ShortcutPattern(
        id="oecd_semantic_aliases",
        path_glob="backend/providers/oecd.py",
        regex=r"KNOWN_INDICATORS|CANONICAL_DATAFLOW_ALIASES|short_code_aliases",
        classification="banned_semantic_final_authority",
        rationale="OECD aliases can map natural-language concepts to dataflows.",
    ),
    ShortcutPattern(
        id="eurostat_dataset_mappings",
        path_glob="backend/providers/eurostat.py",
        regex=r"DATASET_MAPPINGS|_dataset_code|translate_indicator",
        classification="banned_semantic_final_authority",
        rationale="Eurostat mappings/translator can choose datasets from semantic labels.",
    ),
    ShortcutPattern(
        id="imf_supportability_exact_catalog_guard",
        path_glob="backend/utils/imf_supportability.py",
        regex=r"imf_catalog_surface_supportability_reason|_exact_code_candidates",
        classification="fail_closed_supportability",
        rationale="IMF supportability may fail closed from exact provider-native code/catalog evidence, but must not judge arbitrary query text.",
    ),
    ShortcutPattern(
        id="imf_supportability_query_text_marker_regression",
        path_glob="backend/utils/imf_supportability.py",
        regex=r"_DETAIL_MARKERS|_CONSUMER_PRICE_DETAIL_MARKERS|_FISCAL_DETAIL_MARKERS|_NATIONAL_ACCOUNTS_DETAIL_MARKERS|_SOCIAL_DEMOGRAPHIC_DETAIL_MARKERS|_COMPLEX_FINANCE_DETAIL_MARKERS|_SPECIAL_PUBLIC_ENTITY_MARKERS",
        classification="banned_semantic_final_authority",
        rationale="IMF supportability must not reintroduce query-text keyword marker sets as final supportability authority.",
    ),
    ShortcutPattern(
        id="imf_translator_or_direct_mapping",
        path_glob="backend/providers/imf.py",
        regex=r"UNSUPPORTED_INDICATORS|def _indicator_code\(|self\._indicator_code\(|translate_indicator|_resolve_from_local_catalog",
        classification="banned_semantic_final_authority",
        rationale="IMF provider can resolve natural-language indicators through direct maps/catalog/translator.",
    ),
    ShortcutPattern(
        id="bis_translator_or_direct_mapping",
        path_glob="backend/providers/bis.py",
        regex=r"REDIRECT_INDICATORS|def _indicator_code\(|self\._indicator_code\(|translate_indicator|_extract_indicator_keywords",
        classification="banned_semantic_final_authority",
        rationale="BIS provider can resolve natural-language indicators through direct maps/translator/keyword extraction.",
    ),
    ShortcutPattern(
        id="fred_rate_transform_keywords",
        path_glob="backend/providers/fred.py",
        regex=r"_INDEX_SERIES_FOR_RATE|_RATE_KEYWORDS|_infer_transformation",
        classification="candidate_generation_only",
        rationale="FRED rate keywords transform selected index series and need provenance before final authority.",
    ),
)


# Current banned findings that are known debt, not a completion allowlist.
# Ralph is only finished when this ledger is empty and the static scan reports no
# banned semantic final-authority findings.  Keeping this as a named debt ledger
# prevents future work from treating reviewed banned surfaces as acceptable.
CURRENT_BANNED_SEMANTIC_DEBT = frozenset()

# Backward-compatible alias for older tests/imports.  Semantically this is the
# current debt ledger, not a permanent approval list.
REVIEWED_PHASE0_FINDINGS = CURRENT_BANNED_SEMANTIC_DEBT


def iter_scan_paths(root: Path = Path(".")) -> list[Path]:
    """Return files included in the expanded Phase 0 scanner scope."""
    paths: set[Path] = set()
    for glob in SCAN_GLOBS:
        paths.update(path for path in root.glob(glob) if path.is_file())
    return sorted(paths)


def scan_semantic_shortcuts(root: Path = Path(".")) -> list[ShortcutFinding]:
    """Scan reviewed high-risk semantic shortcut patterns."""
    findings: list[ShortcutFinding] = []
    for pattern in PATTERNS:
        for path in sorted(root.glob(pattern.path_glob)):
            if not path.is_file():
                continue
            compiled = re.compile(pattern.regex)
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if compiled.search(line):
                    findings.append(
                        ShortcutFinding(
                            pattern_id=pattern.id,
                            path=path,
                            line_number=line_number,
                            line=line.strip(),
                            classification=pattern.classification,
                            rationale=pattern.rationale,
                        )
                    )
    return findings
