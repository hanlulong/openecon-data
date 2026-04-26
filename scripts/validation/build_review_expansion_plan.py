#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'review_expansion_plan.json'

CLASS_WEIGHTS = {
    'critical': 1.0,
    'high_traffic': 2.0,
}

DESIGN_STRATA_KEYS = {
    'direct': 'direct_provider',
    'multiround': 'multiround_family',
    'ambiguity': 'ambiguity_family',
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)) / denom
    return center - margin


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


def design_confidence(score_report: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(score_report.get('metrics') or {})
    return dict(
        metrics.get('overall_weighted_adjudicated_design_confidence')
        or metrics.get('overall_weighted_design_confidence')
        or {}
    )


def design_stratum_key(dataset_type: str, name: str) -> str:
    return f"{DESIGN_STRATA_KEYS[dataset_type]}:{name}"


def current_n_for_target(
    *,
    current_stats: dict[str, dict[str, Any]],
    design_strata: dict[str, Any],
    dataset_type: str,
    name: str,
) -> int:
    current_n = int(((current_stats.get(name) or {}).get('n')) or 0)
    if current_n > 0:
        return current_n
    stratum = dict(design_strata.get(design_stratum_key(dataset_type, name)) or {})
    return int(stratum.get('n') or stratum.get('rounded_effective_n') or 0)


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


def fixed_share_design_lower(
    states: dict[str, dict[str, Any]],
    additions: dict[str, int],
) -> float:
    lower = 0.0
    for key, state in states.items():
        current_total = int(state['effective_n'])
        current_successes = int(state['effective_successes'])
        additional = int(additions.get(key, 0))
        total = current_total + additional
        successes = current_successes + additional
        stratum_lower = wilson_lower(successes, total)
        if stratum_lower is not None:
            lower += float(state['population_weight_share']) * stratum_lower
    return max(0.0, min(1.0, lower))


def fixed_share_observed_success(
    states: dict[str, dict[str, Any]],
    additions: dict[str, int],
) -> float:
    observed = 0.0
    for key, state in states.items():
        current_total = int(state['effective_n'])
        current_successes = int(state['effective_successes'])
        additional = int(additions.get(key, 0))
        total = current_total + additional
        if total <= 0:
            continue
        observed += float(state['population_weight_share']) * ((current_successes + additional) / total)
    return max(0.0, min(1.0, observed))


def build_design_states(
    *,
    design_strata: dict[str, Any],
    required_by_type: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for dataset_type, group_policy in required_by_type.items():
        for name in group_policy:
            key = design_stratum_key(dataset_type, name)
            stratum = dict(design_strata.get(key) or {})
            if not stratum:
                continue
            effective_n = int(stratum.get('rounded_effective_n') or round(float(stratum.get('effective_n') or 0.0)))
            if effective_n <= 0:
                continue
            weighted_success = float(stratum.get('weighted_success') or 0.0)
            effective_successes = int(stratum.get('effective_successes') or round(weighted_success * effective_n))
            states[key] = {
                'dataset_type': dataset_type,
                'name': name,
                'population_weight_share': float(stratum.get('population_weight_share') or 0.0),
                'effective_n': effective_n,
                'effective_successes': max(0, min(effective_n, effective_successes)),
            }
    return states


def required_design_keys(required_by_type: dict[str, dict[str, dict[str, Any]]]) -> set[str]:
    keys: set[str] = set()
    for dataset_type, group_policy in required_by_type.items():
        for name in group_policy:
            keys.add(design_stratum_key(dataset_type, name))
    return keys


def greedy_design_additions(
    states: dict[str, dict[str, Any]],
    *,
    target_lower95: float,
    target_observed_success: float,
    max_iterations: int = 200_000,
) -> tuple[dict[str, int], float, float]:
    additions = {key: 0 for key in states}
    projected_lower = fixed_share_design_lower(states, additions)
    projected_observed = fixed_share_observed_success(states, additions)

    iterations = 0
    while (
        (projected_lower < target_lower95 or projected_observed < target_observed_success)
        and iterations < max_iterations
    ):
        best_key = None
        best_gain = None
        current_score = min(projected_lower / target_lower95, projected_observed / target_observed_success)
        for key in states:
            trial = dict(additions)
            trial[key] += 1
            trial_lower = fixed_share_design_lower(states, trial)
            trial_observed = fixed_share_observed_success(states, trial)
            trial_score = min(trial_lower / target_lower95, trial_observed / target_observed_success)
            lower_gain = trial_lower - projected_lower
            observed_gain = trial_observed - projected_observed
            gain = (trial_score - current_score, lower_gain, observed_gain, states[key]['population_weight_share'])
            if best_gain is None or gain > best_gain:
                best_gain = gain
                best_key = key
        if best_key is None:
            break
        additions[best_key] += 1
        projected_lower = fixed_share_design_lower(states, additions)
        projected_observed = fixed_share_observed_success(states, additions)
        iterations += 1

    if projected_lower < target_lower95 or projected_observed < target_observed_success:
        raise RuntimeError(
            'Unable to build a design-aware expansion plan that reaches the claim thresholds '
            f'within {max_iterations} projected additional sessions.'
        )

    return additions, projected_lower, projected_observed


def allocate_design_type_plan(
    *,
    dataset_type: str,
    group_policy: dict[str, dict[str, Any]],
    current_stats: dict[str, dict[str, Any]],
    design_strata: dict[str, Any],
    additions_by_stratum: dict[str, int],
) -> dict[str, Any]:
    targets = {}
    total_additional = 0
    for name, policy in group_policy.items():
        key = design_stratum_key(dataset_type, name)
        additional = int(additions_by_stratum.get(key, 0))
        total_additional += additional
        class_name = str(policy.get('class') or 'critical')
        current_n = current_n_for_target(
            current_stats=current_stats,
            design_strata=design_strata,
            dataset_type=dataset_type,
            name=name,
        )
        targets[name] = {
            'class': class_name,
            'floor': float(policy.get('floor') or 0.0),
            'current_n': current_n,
            'additional_target_sessions': additional,
            'recommended_total_n': current_n + additional,
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

    type_counts = current_type_counts(score_report)

    strata = dict(score_report.get('strata') or {})
    direct_policy = required_groups(floor_policy, 'required_direct_provider_floors')
    multiround_policy = required_groups(floor_policy, 'required_multiround_family_floors')
    ambiguity_policy = required_groups(floor_policy, 'required_ambiguity_family_floors')
    required_by_type = {
        'direct': direct_policy,
        'multiround': multiround_policy,
        'ambiguity': ambiguity_policy,
    }
    claim_thresholds = dict((floor_policy or {}).get('claim_thresholds') or {})
    target_lower95 = float(
        gap_report.get('required_lower95')
        or claim_thresholds.get('lower95_min')
        or 0.99
    )
    target_observed_success = float(claim_thresholds.get('weighted_session_success_min') or 0.992)

    design_conf = design_confidence(score_report)
    design_strata = dict(design_conf.get('strata') or {})
    design_states = build_design_states(
        design_strata=design_strata,
        required_by_type=required_by_type,
    )
    missing_design_keys = sorted(required_design_keys(required_by_type) - set(design_states))
    design_plan_metadata: dict[str, Any] | None = None

    if design_states and not missing_design_keys:
        additions_by_stratum, projected_lower95, projected_observed = greedy_design_additions(
            design_states,
            target_lower95=target_lower95,
            target_observed_success=target_observed_success,
        )
        additional_needed = sum(additions_by_stratum.values())
        direct_plan = allocate_design_type_plan(
            dataset_type='direct',
            group_policy=direct_policy,
            current_stats=dict(strata.get('evaluated_provider_floors') or {}),
            design_strata=design_strata,
            additions_by_stratum=additions_by_stratum,
        )
        multiround_plan = allocate_design_type_plan(
            dataset_type='multiround',
            group_policy=multiround_policy,
            current_stats=dict(strata.get('evaluated_multiround_family_floors') or {}),
            design_strata=design_strata,
            additions_by_stratum=additions_by_stratum,
        )
        ambiguity_plan = allocate_design_type_plan(
            dataset_type='ambiguity',
            group_policy=ambiguity_policy,
            current_stats=dict(strata.get('evaluated_ambiguity_family_floors') or {}),
            design_strata=design_strata,
            additions_by_stratum=additions_by_stratum,
        )
        type_budget = {
            'direct': int(direct_plan['additional_target_sessions']),
            'multiround': int(multiround_plan['additional_target_sessions']),
            'ambiguity': int(ambiguity_plan['additional_target_sessions']),
        }
        design_plan_metadata = {
            'enabled': True,
            'method': 'greedy_fixed-share_per_stratum_wilson_projection',
            'current_observed_success': design_conf.get('observed_success'),
            'current_lower95': design_conf.get('lower95'),
            'target_observed_success': target_observed_success,
            'target_lower95': target_lower95,
            'projected_observed_success_after_plan': projected_observed,
            'projected_lower95_after_plan': projected_lower95,
            'additional_target_sessions': additional_needed,
            'missing_required_design_strata': [],
            'caveat': (
                'Projection assumes the added review sessions pass and preserve the design-stratum '
                'population shares represented in the score report; the next score report remains authoritative.'
            ),
        }
    else:
        additional_needed = int(((gap_report.get('gap_estimate') or {}).get('additional_effective_n_needed_at_perfect_success')) or 0)
        type_budget = allocate_integer_budget(
            additional_needed,
            [(name, float(count)) for name, count in sorted(type_counts.items()) if count > 0],
        )
        direct_plan = allocate_type_plan(
            type_budget.get('direct', 0),
            direct_policy,
            dict(strata.get('evaluated_provider_floors') or {}),
        )
        multiround_plan = allocate_type_plan(
            type_budget.get('multiround', 0),
            multiround_policy,
            dict(strata.get('evaluated_multiround_family_floors') or {}),
        )
        ambiguity_plan = allocate_type_plan(
            type_budget.get('ambiguity', 0),
            ambiguity_policy,
            dict(strata.get('evaluated_ambiguity_family_floors') or {}),
        )
        if missing_design_keys:
            design_plan_metadata = {
                'enabled': False,
                'reason': 'missing_required_design_strata',
                'missing_required_design_strata': missing_design_keys,
            }

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
        'design_lower95_expansion': design_plan_metadata,
        'allocation': {
            'by_dataset_type': type_budget,
            'direct': direct_plan,
            'multiround': multiround_plan,
            'ambiguity': ambiguity_plan,
        },
        'notes': [
            (
                'Targets are sized against the design-aware per-stratum lower95 projection when the score report exposes it; '
                'otherwise they fall back to the legacy perfect-pass effective-n gap estimate.'
            ),
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
