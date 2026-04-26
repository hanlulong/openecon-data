#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'review_coverage_progress.json'


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def kish_effective_n(weights: list[float]) -> float | None:
    if not weights:
        return None
    total = sum(weights)
    denom = sum(weight * weight for weight in weights)
    if total <= 0 or denom <= 0:
        return None
    return (total * total) / denom


def pct(current: int | float, target: int | float) -> float | None:
    if target <= 0:
        return None
    return float(current) / float(target)


def progress_row(current_n: int, target_n: int) -> dict[str, Any]:
    return {
        'current_n': current_n,
        'target_n': target_n,
        'remaining_n': max(0, target_n - current_n),
        'progress_ratio': pct(current_n, target_n),
    }


def current_type_counts(score_report: dict[str, Any]) -> dict[str, int]:
    metrics = dict(score_report.get('metrics') or {})
    weighted_counts = dict(metrics.get('weighted_session_counts_by_type') or {})
    if weighted_counts:
        return {str(key): int(value) for key, value in weighted_counts.items()}
    return {
        str(key): int(value)
        for key, value in dict((score_report.get('snapshot') or {}).get('dataset_types') or {}).items()
    }


def aggregate_session_results(score_reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    for report in score_reports:
        for row in list(report.get('session_results') or []):
            session_id = str(row.get('session_id') or '').strip()
            if not session_id or session_id in sessions:
                continue
            sessions[session_id] = row
    return sessions


def aggregate_type_counts(score_reports: list[dict[str, Any]]) -> dict[str, int]:
    sessions = aggregate_session_results(score_reports)
    if sessions:
        counts: dict[str, int] = {}
        for row in sessions.values():
            dataset_type = str(row.get('dataset_type') or '').strip()
            if not dataset_type:
                continue
            counts[dataset_type] = counts.get(dataset_type, 0) + 1
        return counts

    aggregated: dict[str, int] = {}
    for report in score_reports:
        for key, value in current_type_counts(report).items():
            aggregated[key] = aggregated.get(key, 0) + int(value)
    return aggregated


def current_strata_n(score_report: dict[str, Any], strata_key: str) -> dict[str, int]:
    strata = dict(score_report.get('strata') or {})
    stats = dict(strata.get(strata_key) or {})
    return {str(key): int((value or {}).get('n') or 0) for key, value in stats.items()}


def aggregate_strata_n(score_reports: list[dict[str, Any]], dataset_type: str) -> dict[str, int]:
    sessions = aggregate_session_results(score_reports)
    if sessions:
        counts: dict[str, int] = {}
        for row in sessions.values():
            if str(row.get('dataset_type') or '') != dataset_type:
                continue
            if dataset_type == 'direct':
                key = str(row.get('provider_stratum') or '').strip()
            else:
                key = str(row.get('family_stratum') or '').strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    strata_key = {
        'direct': 'evaluated_provider_floors',
        'multiround': 'evaluated_multiround_family_floors',
        'ambiguity': 'evaluated_ambiguity_family_floors',
    }[dataset_type]
    aggregated: dict[str, int] = {}
    for report in score_reports:
        for key, value in current_strata_n(report, strata_key).items():
            aggregated[key] = aggregated.get(key, 0) + int(value)
    return aggregated


def aggregate_effective_n(score_reports: list[dict[str, Any]]) -> float | None:
    sessions = aggregate_session_results(score_reports)
    if not sessions:
        return None
    weights = []
    for row in sessions.values():
        provenance = dict(row.get('provenance') or {})
        weight = provenance.get('selection_weight')
        try:
            weight_value = float(weight)
        except (TypeError, ValueError):
            continue
        if weight_value > 0:
            weights.append(weight_value)
    return kish_effective_n(weights)


def compare_targets(current_stats: dict[str, int], target_stats: dict[str, Any]) -> dict[str, Any]:
    if isinstance(target_stats, list):
        target_stats = {
            str(row.get('name') or ''): {
                **row,
                'recommended_total_n': row.get('recommended_total_n', row.get('target_n')),
                'additional_target_sessions': row.get('additional_target_sessions', row.get('planned_batch_sessions')),
            }
            for row in target_stats
            if str(row.get('name') or '').strip()
        }
    rows = {}
    for name, target in target_stats.items():
        current_n = int(current_stats.get(name, 0))
        target_n = int(target.get('recommended_total_n') or 0)
        row = {
            'class': target.get('class'),
            'floor': target.get('floor'),
            'additional_target_sessions': int(target.get('additional_target_sessions') or 0),
            **progress_row(current_n, target_n),
        }
        rows[name] = row
    return rows


def top_remaining(rows: dict[str, dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    ranked = sorted(
        (
            {
                'name': name,
                **payload,
            }
            for name, payload in rows.items()
        ),
        key=lambda item: (int(item['remaining_n']), str(item['name'])),
        reverse=True,
    )
    return ranked[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description='Track reviewed-coverage progress against the current expansion plan.')
    parser.add_argument('--score-report', action='append', type=Path, required=True)
    parser.add_argument('--expansion-plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    score_reports = [load_json(path.resolve()) for path in args.score_report]
    expansion_plan = load_json(args.expansion_plan.resolve())

    plan_effective_progress = dict(expansion_plan.get('effective_n_progress') or {})
    if plan_effective_progress:
        base_effective_n = float(plan_effective_progress.get('current_effective_n') or 0.0)
        target_effective_n = float(plan_effective_progress.get('target_effective_n') or 0.0)
    else:
        base_effective_n = float(((expansion_plan.get('current') or {}).get('effective_n')) or 0.0)
        additional_needed = float(((expansion_plan.get('current') or {}).get('additional_effective_n_needed')) or 0.0)
        target_effective_n = base_effective_n + additional_needed
    current_effective_n = aggregate_effective_n(score_reports)
    if current_effective_n is None:
        current_effective_n = base_effective_n
    remaining_effective_n = max(0.0, target_effective_n - current_effective_n)
    type_targets = dict(
        ((expansion_plan.get('allocation') or {}).get('by_dataset_type'))
        or expansion_plan.get('dataset_type_batch_allocation')
        or {}
    )
    type_current = aggregate_type_counts(score_reports)

    dataset_type_progress = {}
    for dataset_type in sorted(type_targets):
        current_n = int(type_current.get(dataset_type, 0))
        target_n = current_n + int(type_targets.get(dataset_type, 0) or 0)
        dataset_type_progress[dataset_type] = progress_row(current_n, target_n)

    direct_rows = compare_targets(
        aggregate_strata_n(score_reports, 'direct'),
        (((expansion_plan.get('allocation') or {}).get('direct') or {}).get('targets')) or {},
    )
    multiround_rows = compare_targets(
        aggregate_strata_n(score_reports, 'multiround'),
        (((expansion_plan.get('allocation') or {}).get('multiround') or {}).get('targets')) or {},
    )
    ambiguity_rows = compare_targets(
        aggregate_strata_n(score_reports, 'ambiguity'),
        (((expansion_plan.get('allocation') or {}).get('ambiguity') or {}).get('targets')) or {},
    )

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'score_report_paths': [str(path.resolve()) for path in args.score_report],
        'expansion_plan_path': str(args.expansion_plan.resolve()),
        'snapshot_id': next((report.get('snapshot_id') for report in score_reports if report.get('snapshot_id')), None) or expansion_plan.get('snapshot_id'),
        'effective_n_progress': {
            'current_effective_n': current_effective_n,
            'target_effective_n': target_effective_n,
            'remaining_effective_n': remaining_effective_n,
            'progress_ratio': pct(current_effective_n, target_effective_n),
        },
        'dataset_type_progress': dataset_type_progress,
        'strata_progress': {
            'direct': direct_rows,
            'multiround': multiround_rows,
            'ambiguity': ambiguity_rows,
        },
        'priority_queue': {
            'direct': top_remaining(direct_rows),
            'multiround': top_remaining(multiround_rows),
            'ambiguity': top_remaining(ambiguity_rows),
        },
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(
        json.dumps(
            {
                'output': str(output),
                'remaining_effective_n': remaining_effective_n,
                'effective_n_progress_ratio': payload['effective_n_progress']['progress_ratio'],
            },
            indent=2,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
