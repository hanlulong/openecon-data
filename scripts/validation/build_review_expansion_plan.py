#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'review_expansion_plan.json'

CLASS_WEIGHTS = {
    'critical': 1.0,
    'high_traffic': 2.0,
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def allocate_integer_budget(total: int, weighted_keys: list[tuple[str, float]]) -> dict[str, int]:
    if total <= 0 or not weighted_keys:
        return {key: 0 for key, _ in weighted_keys}
    total_weight = sum(max(weight, 0.0) for _, weight in weighted_keys)
    if total_weight <= 0:
        total_weight = float(len(weighted_keys))
        weighted_keys = [(key, 1.0) for key, _ in weighted_keys]

    raw = {key: (total * max(weight, 0.0) / total_weight) for key, weight in weighted_keys}
    base = {key: int(value) for key, value in raw.items()}
    remainder = total - sum(base.values())
    ranked = sorted(
        weighted_keys,
        key=lambda item: (raw[item[0]] - base[item[0]], item[0]),
        reverse=True,
    )
    for key, _ in ranked[:remainder]:
        base[key] += 1
    return base


def current_type_counts(score_report: dict[str, Any]) -> dict[str, int]:
    metrics = dict(score_report.get('metrics') or {})
    weighted = dict(metrics.get('weighted_session_counts_by_type') or {})
    if weighted:
        return {str(key): int(value) for key, value in weighted.items()}
    return {
        str(key): int(value)
        for key, value in dict((score_report.get('snapshot') or {}).get('dataset_types') or {}).items()
    }


def required_groups(floor_policy: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {
        str(name): dict(policy or {})
        for name, policy in dict(floor_policy.get(key) or {}).items()
    }


def allocate_type_plan(
    total_additional: int,
    group_policy: dict[str, dict[str, Any]],
    current_stats: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    weighted_keys = []
    for name, policy in group_policy.items():
        class_name = str(policy.get('class') or 'critical')
        weighted_keys.append((name, CLASS_WEIGHTS.get(class_name, 1.0)))

    additional = allocate_integer_budget(total_additional, weighted_keys)
    targets = {}
    for name, policy in group_policy.items():
        current_n = int(((current_stats.get(name) or {}).get('n')) or 0)
        class_name = str(policy.get('class') or 'critical')
        targets[name] = {
            'class': class_name,
            'floor': float(policy.get('floor') or 0.0),
            'current_n': current_n,
            'additional_target_sessions': additional.get(name, 0),
            'recommended_total_n': current_n + additional.get(name, 0),
        }
    return {
        'additional_target_sessions': total_additional,
        'targets': targets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Build an actionable reviewed-coverage expansion plan from the current score and gap reports.')
    parser.add_argument('--score-report', type=Path, required=True)
    parser.add_argument('--gap-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    score_report = load_json(args.score_report.resolve())
    gap_report = load_json(args.gap_report.resolve())
    floor_policy_path = Path(str(score_report['floor_policy_path'])).resolve()
    floor_policy = load_json(floor_policy_path)

    additional_needed = int(((gap_report.get('gap_estimate') or {}).get('additional_effective_n_needed_at_perfect_success')) or 0)
    type_counts = current_type_counts(score_report)
    type_budget = allocate_integer_budget(
        additional_needed,
        [(name, float(count)) for name, count in sorted(type_counts.items()) if count > 0],
    )

    strata = dict(score_report.get('strata') or {})
    direct_policy = required_groups(floor_policy, 'required_direct_provider_floors')
    multiround_policy = required_groups(floor_policy, 'required_multiround_family_floors')
    ambiguity_policy = required_groups(floor_policy, 'required_ambiguity_family_floors')

    plan = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'snapshot_id': score_report.get('snapshot_id'),
        'score_report_path': str(args.score_report.resolve()),
        'gap_report_path': str(args.gap_report.resolve()),
        'current': {
            'effective_n': ((gap_report.get('current') or {}).get('overall_weighted_effective_n')),
            'claim_lower95': ((gap_report.get('current') or {}).get('claim_lower95')),
            'additional_effective_n_needed': additional_needed,
            'type_counts': type_counts,
        },
        'allocation': {
            'by_dataset_type': type_budget,
            'direct': allocate_type_plan(
                type_budget.get('direct', 0),
                direct_policy,
                dict(strata.get('evaluated_provider_floors') or {}),
            ),
            'multiround': allocate_type_plan(
                type_budget.get('multiround', 0),
                multiround_policy,
                dict(strata.get('evaluated_multiround_family_floors') or {}),
            ),
            'ambiguity': allocate_type_plan(
                type_budget.get('ambiguity', 0),
                ambiguity_policy,
                dict(strata.get('evaluated_ambiguity_family_floors') or {}),
            ),
        },
        'notes': [
            'Targets are sized against the perfect-pass lower95 gap estimate; real nominal needs may be higher if future reviewed sessions use unequal weights or introduce clustering.',
            'High-traffic strata are up-weighted 2x relative to critical strata to preserve stronger evidence on the most consequential surfaces.',
            'This plan assumes current stratum coverage remains representative and should be revised after each substantial reviewed-bundle expansion.',
        ],
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'additional_effective_n_needed': additional_needed}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
