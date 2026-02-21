#!/usr/bin/env python3
"""
Framework benchmark runner for OpenEcon query intelligence.

Measures two core dimensions:
1. Routing accuracy (query -> provider) using deterministic router.
2. Series matching accuracy (query + provider -> indicator selection quality) using IndicatorResolver.

Design goals:
- Deterministic and local (no API calls required).
- Regression-friendly (JSON output + threshold-based exit code).
- General framework coverage (catalog-driven series cases, not one-off patches).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.routing.unified_router import UnifiedRouter  # noqa: E402
from backend.services.catalog_service import (  # noqa: E402
    get_all_synonyms,
    get_exclusions,
    get_indicator_codes,
    load_catalog,
)
from backend.services.indicator_resolver import get_indicator_resolver  # noqa: E402


@dataclass
class RoutingCase:
    query: str
    expected_provider: str
    case_id: str


@dataclass
class RoutingResult:
    case_id: str
    query: str
    expected_provider: str
    predicted_provider: str
    passed: bool


@dataclass
class SeriesCase:
    concept: str
    query: str
    provider: str
    expected_primary_code: str
    expected_codes: List[str]
    term_type: str  # primary | secondary


@dataclass
class SeriesResult:
    concept: str
    query: str
    provider: str
    expected_primary_code: str
    expected_codes: List[str]
    predicted_code: Optional[str]
    predicted_provider: Optional[str]
    predicted_name: Optional[str]
    passed: bool
    pass_reason: str
    confidence: Optional[float]
    source: Optional[str]
    provider_match: bool
    exact_code_match: bool
    concept_match: bool
    predicted_concept: Optional[str]
    concept_score: float


def normalize_provider(provider: str) -> str:
    """Normalize provider names for comparison."""
    if not provider:
        return ""
    p = provider.strip().upper().replace(" ", "")
    alias_map = {
        "WORLD BANK": "WORLDBANK",
        "WORLDBANK": "WORLDBANK",
        "STATSCAN": "STATSCAN",
        "STATISTICSCANADA": "STATSCAN",
        "EXCHANGE_RATE": "EXCHANGERATE",
        "EXCHANGERATE": "EXCHANGERATE",
        "COIN_GECKO": "COINGECKO",
        "COINGECKO": "COINGECKO",
    }
    return alias_map.get(p, p)


def normalize_code(code: Optional[str]) -> str:
    """Normalize code values for case-insensitive comparisons."""
    if not code:
        return ""
    return str(code).strip().upper()


def tokenize_terms(text: str) -> Set[str]:
    """Tokenize text for lightweight semantic concept matching."""
    import re

    if not text:
        return set()

    stop_words = {
        "the", "a", "an", "of", "for", "in", "to", "and", "or",
        "show", "get", "find", "data", "series", "indicator", "rate",
        "index", "value", "values", "percent", "percentage",
        "country", "countries", "from", "with", "by", "on", "at",
    }
    raw_terms = set(re.findall(r"[a-z0-9]+", text.lower()))
    terms: Set[str] = set()
    for term in raw_terms:
        if len(term) <= 1 or term in stop_words:
            continue
        terms.add(term)
        if term.endswith("ies") and len(term) > 4:
            terms.add(term[:-3] + "y")
        elif term.endswith("s") and len(term) > 3:
            terms.add(term[:-1])
    return terms


def build_concept_profiles(catalog: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build per-concept lexical profiles for concept-level scoring."""
    profiles: Dict[str, Dict[str, Any]] = {}
    for concept_name in catalog.keys():
        synonyms = [s for s in get_all_synonyms(concept_name) if str(s).strip()]
        exclusions = [e for e in get_exclusions(concept_name) if str(e).strip()]

        concept_terms: Set[str] = set()
        for synonym in synonyms:
            concept_terms.update(tokenize_terms(str(synonym)))

        profiles[concept_name] = {
            "synonyms": [str(s).strip().lower() for s in synonyms],
            "exclusions": [str(e).strip().lower() for e in exclusions],
            "terms": concept_terms,
        }

    return profiles


def score_concept_match(
    concept_name: str,
    text: str,
    concept_profiles: Dict[str, Dict[str, Any]],
) -> float:
    """
    Score semantic alignment of text to a catalog concept on a 0-1 scale.
    """
    if not text or concept_name not in concept_profiles:
        return 0.0

    profile = concept_profiles[concept_name]
    text_lower = text.lower()
    candidate_terms = tokenize_terms(text_lower)

    for exclusion in profile["exclusions"]:
        if exclusion and exclusion in text_lower:
            return 0.0

    phrase_hits = 0
    for synonym in profile["synonyms"]:
        if len(synonym) < 3:
            continue
        synonym_terms = tokenize_terms(synonym)
        if synonym in text_lower or (synonym_terms and synonym_terms <= candidate_terms):
            phrase_hits += 1

    overlap_count = len(profile["terms"] & candidate_terms)
    recall = overlap_count / max(len(profile["terms"]), 1)
    precision = overlap_count / max(len(candidate_terms), 1)
    overlap = max(recall, precision)

    score = 0.0
    if phrase_hits:
        score += min(0.70, 0.25 + 0.15 * phrase_hits)
    score += min(0.35, overlap * 0.55)
    return max(0.0, min(1.0, score))


def infer_best_concept(
    text: str,
    concept_profiles: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[str], float]:
    """Infer most likely concept label for resolved indicator text."""
    best_concept: Optional[str] = None
    best_score = 0.0

    for concept_name in concept_profiles.keys():
        score = score_concept_match(concept_name, text, concept_profiles)
        if score > best_score:
            best_score = score
            best_concept = concept_name

    if best_score < 0.20:
        return None, best_score
    return best_concept, best_score


def load_routing_cases_from_python(path: Path) -> List[RoutingCase]:
    """
    Load routing cases from a Python file that contains QUERIES = [ ... ] literal.

    Uses AST parsing to avoid importing runtime dependencies from the source file.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    queries_literal = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "QUERIES":
                    queries_literal = ast.literal_eval(node.value)
                    break
        if queries_literal is not None:
            break

    if queries_literal is None:
        raise ValueError(f"Could not find QUERIES assignment in {path}")

    cases: List[RoutingCase] = []
    for item in queries_literal:
        query = str(item.get("query", "")).strip()
        provider = str(item.get("provider", "")).strip()
        case_id = str(item.get("id", len(cases) + 1))
        if not query or not provider:
            continue
        cases.append(
            RoutingCase(
                query=query,
                expected_provider=provider,
                case_id=f"R{case_id}",
            )
        )
    return cases


def build_series_cases_from_catalog(include_secondary: bool = True) -> List[SeriesCase]:
    """
    Build series matching benchmark cases from catalog concept definitions.

    For each concept/provider pair, create:
    - one primary synonym query case
    - optionally one secondary synonym query case
    """
    catalog = load_catalog()
    cases: List[SeriesCase] = []

    for concept_name, concept_data in catalog.items():
        providers = concept_data.get("providers", {}) or {}
        synonyms = concept_data.get("synonyms", {}) or {}
        primary_synonyms = [s.strip() for s in (synonyms.get("primary", []) or []) if str(s).strip()]
        secondary_synonyms = [s.strip() for s in (synonyms.get("secondary", []) or []) if str(s).strip()]

        if primary_synonyms:
            primary_query = primary_synonyms[0]
        else:
            primary_query = concept_name.replace("_", " ")

        secondary_query = None
        if include_secondary:
            for s in secondary_synonyms:
                if s.lower() != primary_query.lower():
                    secondary_query = s
                    break

        for provider_name, provider_info in providers.items():
            primary_info = provider_info.get("primary", {}) or {}
            expected_primary_code = primary_info.get("code")
            if not expected_primary_code:
                continue

            expected_codes = get_indicator_codes(concept_name, provider_name)
            if expected_primary_code not in expected_codes:
                expected_codes = [str(expected_primary_code)] + expected_codes

            cases.append(
                SeriesCase(
                    concept=concept_name,
                    query=primary_query,
                    provider=provider_name,
                    expected_primary_code=str(expected_primary_code),
                    expected_codes=[str(c) for c in expected_codes if str(c).strip()],
                    term_type="primary",
                )
            )

            if secondary_query:
                cases.append(
                    SeriesCase(
                        concept=concept_name,
                        query=secondary_query,
                        provider=provider_name,
                        expected_primary_code=str(expected_primary_code),
                        expected_codes=[str(c) for c in expected_codes if str(c).strip()],
                        term_type="secondary",
                    )
                )

    return cases


def run_routing_benchmark(cases: List[RoutingCase]) -> Dict[str, Any]:
    router = UnifiedRouter()
    results: List[RoutingResult] = []
    provider_totals: Dict[str, Dict[str, int]] = {}

    for case in cases:
        decision = router.route(case.query)
        predicted = normalize_provider(decision.provider)
        expected = normalize_provider(case.expected_provider)
        passed = predicted == expected

        results.append(
            RoutingResult(
                case_id=case.case_id,
                query=case.query,
                expected_provider=expected,
                predicted_provider=predicted,
                passed=passed,
            )
        )

        bucket = provider_totals.setdefault(expected, {"total": 0, "passed": 0})
        bucket["total"] += 1
        if passed:
            bucket["passed"] += 1

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    accuracy = (passed / total) if total else 0.0

    failures = [asdict(r) for r in results if not r.passed][:25]
    per_provider = {
        provider: {
            "total": stats["total"],
            "passed": stats["passed"],
            "accuracy": (stats["passed"] / stats["total"]) if stats["total"] else 0.0,
        }
        for provider, stats in sorted(provider_totals.items(), key=lambda kv: kv[0])
    }

    return {
        "total_cases": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": accuracy,
        "per_provider": per_provider,
        "sample_failures": failures,
    }


def run_series_benchmark(cases: List[SeriesCase], strict_code_match: bool = False) -> Dict[str, Any]:
    resolver = get_indicator_resolver()
    concept_profiles = build_concept_profiles(load_catalog())
    results: List[SeriesResult] = []
    term_type_totals: Dict[str, Dict[str, int]] = {"primary": {"total": 0, "passed": 0}, "secondary": {"total": 0, "passed": 0}}
    reason_totals: Dict[str, int] = {"exact_code": 0, "concept_match": 0, "failed": 0}

    for case in cases:
        resolved = resolver.resolve(case.query, provider=case.provider, use_cache=False)
        predicted_code = resolved.code if resolved else None
        predicted_provider = resolved.provider if resolved else None
        predicted_name = resolved.name if resolved else None
        confidence = resolved.confidence if resolved else None
        source = resolved.source if resolved else None
        provider_match = normalize_provider(predicted_provider or "") == normalize_provider(case.provider)

        expected_code_set = {normalize_code(c) for c in case.expected_codes if c}
        predicted_code_norm = normalize_code(predicted_code)
        exact_code_match = bool(predicted_code_norm) and predicted_code_norm in expected_code_set

        predicted_text = " ".join(
            [
                str(predicted_name or ""),
                str(predicted_code or ""),
                str((resolved.metadata or {}).get("description", "") if resolved else ""),
            ]
        ).strip()
        predicted_concept, concept_score = infer_best_concept(predicted_text, concept_profiles)
        concept_match = (
            provider_match and predicted_concept == case.concept and concept_score >= 0.25
        )

        if strict_code_match:
            passed = provider_match and exact_code_match
            pass_reason = "exact_code" if passed else "failed"
        else:
            if provider_match and exact_code_match:
                passed = True
                pass_reason = "exact_code"
            elif concept_match:
                passed = True
                pass_reason = "concept_match"
            else:
                passed = False
                pass_reason = "failed"

        reason_totals[pass_reason] = reason_totals.get(pass_reason, 0) + 1
        results.append(
            SeriesResult(
                concept=case.concept,
                query=case.query,
                provider=case.provider,
                expected_primary_code=case.expected_primary_code,
                expected_codes=case.expected_codes,
                predicted_code=predicted_code,
                predicted_provider=predicted_provider,
                predicted_name=predicted_name,
                passed=passed,
                pass_reason=pass_reason,
                confidence=confidence,
                source=source,
                provider_match=provider_match,
                exact_code_match=exact_code_match,
                concept_match=concept_match,
                predicted_concept=predicted_concept,
                concept_score=concept_score,
            )
        )

        bucket = term_type_totals.setdefault(case.term_type, {"total": 0, "passed": 0})
        bucket["total"] += 1
        if passed:
            bucket["passed"] += 1

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    no_result = sum(1 for r in results if r.predicted_code is None)
    accuracy = (passed / total) if total else 0.0

    failures = [asdict(r) for r in results if not r.passed][:25]
    by_term_type = {
        term_type: {
            "total": stats["total"],
            "passed": stats["passed"],
            "accuracy": (stats["passed"] / stats["total"]) if stats["total"] else 0.0,
        }
        for term_type, stats in term_type_totals.items()
    }

    return {
        "total_cases": total,
        "passed": passed,
        "failed": total - passed,
        "no_result": no_result,
        "accuracy": accuracy,
        "strict_code_match": strict_code_match,
        "exact_code_matches": reason_totals.get("exact_code", 0),
        "concept_matches": reason_totals.get("concept_match", 0),
        "by_term_type": by_term_type,
        "sample_failures": failures,
    }


def print_summary(report: Dict[str, Any]) -> None:
    routing = report["routing"]
    series = report["series"]

    print("\n=== OpenEcon Query Framework Benchmark ===")
    print(f"Timestamp: {report['timestamp']}")
    print("\nRouting Accuracy")
    print(
        f"- Passed: {routing['passed']}/{routing['total_cases']} "
        f"({routing['accuracy'] * 100:.1f}%)"
    )
    print("\nSeries Matching Accuracy")
    print(
        f"- Passed: {series['passed']}/{series['total_cases']} "
        f"({series['accuracy'] * 100:.1f}%)"
    )
    print(f"- No result: {series['no_result']}")
    mode = "strict code match" if series.get("strict_code_match") else "concept-aware"
    print(f"- Mode: {mode}")
    print(
        f"- Pass breakdown: exact_code={series.get('exact_code_matches', 0)}, "
        f"concept_match={series.get('concept_matches', 0)}"
    )

    if routing["sample_failures"]:
        print("\nTop Routing Failures (sample)")
        for f in routing["sample_failures"][:5]:
            print(
                f"- {f['case_id']}: expected={f['expected_provider']} "
                f"got={f['predicted_provider']} | {f['query'][:90]}"
            )

    if series["sample_failures"]:
        print("\nTop Series Failures (sample)")
        for f in series["sample_failures"][:5]:
            print(
                f"- concept={f['concept']} provider={f['provider']} "
                f"expected={f['expected_primary_code']} got={f['predicted_code']} "
                f"(reason={f['pass_reason']}) | query='{f['query']}'"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark OpenEcon query framework accuracy")
    parser.add_argument(
        "--routing-source",
        default="tests/comprehensive_100_queries.py",
        help="Python file containing QUERIES literal with expected provider labels",
    )
    parser.add_argument(
        "--routing-limit",
        type=int,
        default=0,
        help="Limit routing cases (0 = all)",
    )
    parser.add_argument(
        "--series-limit",
        type=int,
        default=0,
        help="Limit series cases (0 = all)",
    )
    parser.add_argument(
        "--no-secondary-synonyms",
        action="store_true",
        help="Only benchmark primary synonym series cases",
    )
    parser.add_argument(
        "--strict-series-code-match",
        action="store_true",
        help="Require exact catalog code match for series benchmark (disables concept-aware pass mode)",
    )
    parser.add_argument(
        "--min-routing-accuracy",
        type=float,
        default=0.85,
        help="Fail if routing accuracy falls below this threshold (0-1)",
    )
    parser.add_argument(
        "--min-series-accuracy",
        type=float,
        default=0.90,
        help="Fail if series matching accuracy falls below this threshold (0-1)",
    )
    parser.add_argument(
        "--output",
        default="tests/benchmark_report.latest.json",
        help="Path to write JSON report",
    )

    args = parser.parse_args()

    routing_source = (REPO_ROOT / args.routing_source).resolve()
    if not routing_source.exists():
        raise FileNotFoundError(f"Routing source not found: {routing_source}")

    routing_cases = load_routing_cases_from_python(routing_source)
    series_cases = build_series_cases_from_catalog(include_secondary=not args.no_secondary_synonyms)

    if args.routing_limit > 0:
        routing_cases = routing_cases[: args.routing_limit]
    if args.series_limit > 0:
        series_cases = series_cases[: args.series_limit]

    routing_report = run_routing_benchmark(routing_cases)
    series_report = run_series_benchmark(
        series_cases,
        strict_code_match=args.strict_series_code_match,
    )

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "routing": routing_report,
        "series": series_report,
        "thresholds": {
            "min_routing_accuracy": args.min_routing_accuracy,
            "min_series_accuracy": args.min_series_accuracy,
        },
        "inputs": {
            "routing_source": str(routing_source),
            "routing_cases": len(routing_cases),
            "series_cases": len(series_cases),
            "include_secondary_synonyms": not args.no_secondary_synonyms,
            "strict_series_code_match": args.strict_series_code_match,
        },
    }

    print_summary(report)

    output_path = (REPO_ROOT / args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved report: {output_path}")

    routing_ok = routing_report["accuracy"] >= args.min_routing_accuracy
    series_ok = series_report["accuracy"] >= args.min_series_accuracy

    if routing_ok and series_ok:
        print("\n✅ Benchmark passed thresholds")
        return 0

    print("\n❌ Benchmark failed thresholds")
    if not routing_ok:
        print(
            f"- Routing accuracy {routing_report['accuracy']:.3f} "
            f"< required {args.min_routing_accuracy:.3f}"
        )
    if not series_ok:
        print(
            f"- Series accuracy {series_report['accuracy']:.3f} "
            f"< required {args.min_series_accuracy:.3f}"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
