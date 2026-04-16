#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'claim_decision.json'
DEFAULT_REQUIRED_OBSERVED = 0.992
DEFAULT_REQUIRED_LOWER95 = 0.99


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)) / denom
    return center - margin


def nested_get(obj: dict, *keys):
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def metric_with_proxy(report: dict[str, Any], exact_key: str, proxy_key: str) -> float | None:
    value = nested_get(report, 'metrics', exact_key)
    if value is None:
        value = nested_get(report, 'metrics', proxy_key)
    return value


def triage_framework_clusters(triage_report: dict[str, Any] | None) -> dict[str, int]:
    if triage_report is None:
        return {}
    summary_clusters = dict((triage_report.get('summary') or {}).get('framework_bug_cluster_counts') or {})
    if summary_clusters:
        return {str(key): int(value) for key, value in summary_clusters.items()}
    counts: dict[str, int] = {}
    for item in list(triage_report.get('items') or []):
        if item.get('bucket') != 'likely_framework_bug':
            continue
        cluster = str(item.get('cluster') or '').strip()
        if not cluster:
            continue
        counts[cluster] = counts.get(cluster, 0) + 1
    return counts


def parity_material_drift_count(parity_report: dict[str, Any] | None) -> int:
    if parity_report is None:
        return 0
    severity_counts = dict((parity_report.get('summary') or {}).get('severity_counts') or {})
    return int(severity_counts.get('material') or 0)


def claim_observed_success(report: dict[str, Any]) -> float:
    session_results = list(report.get('session_results') or [])
    successes = sum(1 for row in session_results if row.get('provisional_structural_pass'))
    total = len(session_results)
    observed = nested_get(report, 'metrics', 'claim_observed_success')
    if observed is None:
        observed = nested_get(report, 'metrics', 'overall_weighted_provisional_success')
    if observed is None:
        observed = nested_get(report, 'metrics', 'direct_weighted_provisional_success')
    if observed is None:
        observed = successes / total if total else 0.0
    return float(observed)


def claim_lower95_value(report: dict[str, Any]) -> float:
    session_results = list(report.get('session_results') or [])
    successes = sum(1 for row in session_results if row.get('provisional_structural_pass'))
    total = len(session_results)
    lower95 = nested_get(report, 'metrics', 'claim_lower95')
    if lower95 is None:
        lower95 = nested_get(report, 'metrics', 'overall_weighted_lower95_approx')
    if lower95 is None:
        lower95 = nested_get(report, 'metrics', 'direct_weighted_lower95_approx')
    if lower95 is None:
        lower95 = wilson_lower(successes, total)
    return float(lower95)


def prefixed(label: str, message: str) -> str:
    return f'{label} {message}' if label else message


def append_semantic_threshold_blockers(
    blockers: list[str],
    report: dict[str, Any],
    claim_thresholds: dict[str, Any],
    *,
    label: str = '',
) -> None:
    if report.get('scoring_mode') != 'claim_grade':
        return
    wrong_confident_answer_rate = metric_with_proxy(report, 'wrong_confident_answer_rate', 'wrong_confident_answer_rate_proxy')
    unnecessary_clarification_rate = metric_with_proxy(report, 'unnecessary_clarification_rate', 'unnecessary_clarification_rate_proxy')
    ambiguity_resolution_success = metric_with_proxy(report, 'ambiguity_resolution_success', 'ambiguity_resolution_success_proxy')

    wrong_confident_max = claim_thresholds.get('wrong_confident_answer_rate_max')
    unnecessary_clarification_max = claim_thresholds.get('unnecessary_clarification_rate_max')
    ambiguity_resolution_min = claim_thresholds.get('ambiguity_resolution_success_min')
    if wrong_confident_max is not None:
        if wrong_confident_answer_rate is None:
            blockers.append(prefixed(label, 'wrong_confident_answer_rate missing'))
        elif wrong_confident_answer_rate > float(wrong_confident_max):
            blockers.append(
                prefixed(
                    label,
                    f'wrong_confident_answer_rate {wrong_confident_answer_rate:.6f} is above required {float(wrong_confident_max):.6f}',
                )
            )
    if unnecessary_clarification_max is not None:
        if unnecessary_clarification_rate is None:
            blockers.append(prefixed(label, 'unnecessary_clarification_rate missing'))
        elif unnecessary_clarification_rate > float(unnecessary_clarification_max):
            blockers.append(
                prefixed(
                    label,
                    f'unnecessary_clarification_rate {unnecessary_clarification_rate:.6f} is above required {float(unnecessary_clarification_max):.6f}',
                )
            )
    if ambiguity_resolution_min is not None:
        if ambiguity_resolution_success is None:
            blockers.append(prefixed(label, 'ambiguity_resolution_success missing'))
        elif ambiguity_resolution_success < float(ambiguity_resolution_min):
            blockers.append(
                prefixed(
                    label,
                    f'ambiguity_resolution_success {ambiguity_resolution_success:.6f} is below required {float(ambiguity_resolution_min):.6f}',
                )
            )


def main() -> int:
    parser = argparse.ArgumentParser(description='Apply a conservative claim gate to a certification score report.')
    parser.add_argument('--score-report', type=Path, required=True)
    parser.add_argument('--adjudication-summary', type=Path, default=None)
    parser.add_argument('--production-score-report', type=Path, default=None)
    parser.add_argument('--triage-report', type=Path, default=None)
    parser.add_argument('--parity-report', type=Path, default=None)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--required-observed', type=float, default=None)
    parser.add_argument('--required-lower95', type=float, default=None)
    args = parser.parse_args()

    report = json.loads(args.score_report.resolve().read_text(encoding='utf-8'))
    adjudication_summary = None
    if args.adjudication_summary is not None and args.adjudication_summary.exists():
        adjudication_summary = json.loads(args.adjudication_summary.resolve().read_text(encoding='utf-8'))
    production_score_report = None
    if args.production_score_report is not None and args.production_score_report.exists():
        production_score_report = json.loads(args.production_score_report.resolve().read_text(encoding='utf-8'))
    triage_report = None
    if args.triage_report is not None and args.triage_report.exists():
        triage_report = json.loads(args.triage_report.resolve().read_text(encoding='utf-8'))
    parity_report = None
    if args.parity_report is not None and args.parity_report.exists():
        parity_report = json.loads(args.parity_report.resolve().read_text(encoding='utf-8'))
    floor_policy_path = Path(str(report.get('floor_policy_path'))).resolve() if report.get('floor_policy_path') else None
    floor_policy = load_json(floor_policy_path)
    observed = claim_observed_success(report)
    lower95 = claim_lower95_value(report)
    failing_strata = list(nested_get(report, 'strata', 'failing_strata') or [])
    missing_required_strata = list(nested_get(report, 'strata', 'missing_required_strata') or [])
    wrong_confident_answer_rate = metric_with_proxy(report, 'wrong_confident_answer_rate', 'wrong_confident_answer_rate_proxy')
    unnecessary_clarification_rate = metric_with_proxy(report, 'unnecessary_clarification_rate', 'unnecessary_clarification_rate_proxy')
    ambiguity_resolution_success = metric_with_proxy(report, 'ambiguity_resolution_success', 'ambiguity_resolution_success_proxy')
    claim_thresholds = dict((floor_policy or {}).get('claim_thresholds') or {})
    required_observed = (
        args.required_observed
        if args.required_observed is not None
        else float(claim_thresholds.get('weighted_session_success_min', DEFAULT_REQUIRED_OBSERVED))
    )
    required_lower95 = (
        args.required_lower95
        if args.required_lower95 is not None
        else float(claim_thresholds.get('lower95_min', DEFAULT_REQUIRED_LOWER95))
    )

    blockers = []
    if report.get('scoring_mode') != 'claim_grade':
        blockers.append('scoring_mode is not claim_grade')
    if not report.get('claim_grade_ready', False):
        blockers.append('score report is not marked claim_grade_ready')
        report_blockers = list(report.get('claim_grade_blockers') or [])
        blockers.extend(f'score report blocker: {item}' for item in report_blockers)
    if not report.get('snapshot_id'):
        blockers.append('snapshot_id missing from score report')
    if observed < required_observed:
        blockers.append(f'observed success {observed:.6f} is below required {required_observed:.6f}')
    if lower95 < required_lower95:
        blockers.append(f'lower95 {lower95:.6f} is below required {required_lower95:.6f}')
    if failing_strata:
        blockers.append(f'required strata failed: {", ".join(str(item) for item in failing_strata)}')
    if missing_required_strata:
        blockers.append(f'required strata missing: {", ".join(str(item) for item in missing_required_strata)}')
    append_semantic_threshold_blockers(blockers, report, claim_thresholds)
    if adjudication_summary is None:
        blockers.append('adjudication summary missing')
    elif not adjudication_summary.get('adjudication_complete', False):
        blockers.append('adjudication is not complete')
    framework_clusters = triage_framework_clusters(triage_report)
    if triage_report is None:
        blockers.append('triage report missing')
    elif framework_clusters:
        blockers.append(
            'unresolved framework failure clusters present: '
            + ', '.join(f'{cluster}={count}' for cluster, count in sorted(framework_clusters.items()))
        )
    if production_score_report is None:
        blockers.append('production score report missing')
    else:
        if production_score_report.get('snapshot_id') != report.get('snapshot_id'):
            blockers.append('production snapshot_id does not match local score report')
        if production_score_report.get('floor_policy_sha256') != report.get('floor_policy_sha256'):
            blockers.append('production floor policy does not match local score report')
        production_observed = claim_observed_success(production_score_report)
        production_lower95 = claim_lower95_value(production_score_report)
        if production_observed < required_observed:
            blockers.append(f'production observed success {production_observed:.6f} is below required {required_observed:.6f}')
        if production_lower95 < required_lower95:
            blockers.append(f'production lower95 {production_lower95:.6f} is below required {required_lower95:.6f}')
        append_semantic_threshold_blockers(blockers, production_score_report, claim_thresholds, label='production')
        if not production_score_report.get('claim_grade_ready', False):
            blockers.append('production score report is not marked claim_grade_ready')
            production_blockers = list(production_score_report.get('claim_grade_blockers') or [])
            blockers.extend(f'production score blocker: {item}' for item in production_blockers)
        if parity_report is None:
            blockers.append('parity report missing')
        else:
            material_drift = parity_material_drift_count(parity_report)
            if material_drift > 0:
                blockers.append(f'production parity has {material_drift} material drift session(s)')

    decision = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'score_report': str(args.score_report.resolve()),
        'adjudication_summary': str(args.adjudication_summary.resolve()) if args.adjudication_summary else None,
        'production_score_report': str(args.production_score_report.resolve()) if args.production_score_report else None,
        'triage_report': str(args.triage_report.resolve()) if args.triage_report else None,
        'parity_report': str(args.parity_report.resolve()) if args.parity_report else None,
        'floor_policy_path': str(floor_policy_path) if floor_policy_path is not None else None,
        'observed_success': observed,
        'lower95': lower95,
        'required_observed': required_observed,
        'required_lower95': required_lower95,
        'wrong_confident_answer_rate': wrong_confident_answer_rate,
        'unnecessary_clarification_rate': unnecessary_clarification_rate,
        'ambiguity_resolution_success': ambiguity_resolution_success,
        'claim_allowed': len(blockers) == 0,
        'blockers': blockers,
        'failing_strata': failing_strata,
        'missing_required_strata': missing_required_strata,
        'framework_bug_cluster_counts': framework_clusters,
        'parity_material_drift_sessions': parity_material_drift_count(parity_report) if parity_report is not None else None,
        'note': 'This gate is intentionally conservative and will refuse a catalog-wide claim until claim-grade scoring and adjudication are in place.',
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(decision, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(decision, indent=2))
    return 0 if decision['claim_allowed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
