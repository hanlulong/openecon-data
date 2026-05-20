#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'review_expansion_plan.json'
DEFAULT_DB = ROOT / 'backend' / 'data' / 'indicators.db'

try:
    from backend.utils.imf_supportability import imf_catalog_sampler_supportability_reason
except Exception:  # pragma: no cover - allows lightweight unit fixtures
    def imf_catalog_sampler_supportability_reason(*_args: Any, **_kwargs: Any) -> str | None:
        return None

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
        additional = int(additions.get(key, 0))
        if 'raw_weight_total' in state:
            projected = projected_raw_weight_stratum(state, additional)
            stratum_lower = projected['lower95']
        else:
            current_total = int(state['effective_n'])
            current_successes = int(state['effective_successes'])
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
        additional = int(additions.get(key, 0))
        if 'raw_weight_total' in state:
            projected = projected_raw_weight_stratum(state, additional)
            pass_rate = projected['weighted_success']
        else:
            current_total = int(state['effective_n'])
            current_successes = int(state['effective_successes'])
            total = current_total + additional
            if total <= 0:
                continue
            pass_rate = (current_successes + additional) / total
        observed += float(state['population_weight_share']) * pass_rate
    return max(0.0, min(1.0, observed))


def projected_future_weight_components(state: dict[str, Any], additional: int) -> dict[str, float]:
    """Return raw-weight components for all-pass future review rows.

    The claim scorer computes per-stratum Kish effective n from raw selection
    weights, then rescales those weights to fixed frozen-population shares.  A
    review expansion therefore cannot be projected by treating every added row
    as +1 effective n.  This function mirrors the materializers' mechanical
    sampling-weight formulas so planning uses the same math the scorer will
    later apply.
    """
    if additional <= 0:
        return {'weight_total': 0.0, 'weight_square_total': 0.0}

    model = str(state.get('future_weight_model') or '')
    if model == 'direct_catalog_population':
        population = float(state.get('future_population_count') or 0.0)
        if population > 0:
            return {
                'weight_total': population,
                'weight_square_total': (population * population) / additional,
            }

    if model == 'template_target_total':
        current_n = int(state.get('future_current_n') or 0)
        target_total = float(max(current_n + additional, additional))
        return {
            'weight_total': target_total,
            'weight_square_total': (target_total * target_total) / additional,
        }

    # Defensive fallback for lightweight fixtures or future materializers that
    # use one uniform review row as one raw-weight unit.
    return {
        'weight_total': float(additional),
        'weight_square_total': float(additional),
    }


def projected_raw_weight_stratum(state: dict[str, Any], additional: int) -> dict[str, float | int | None]:
    future = projected_future_weight_components(state, additional)
    total = float(state.get('raw_weight_total') or 0.0) + float(future['weight_total'])
    square_total = float(state.get('raw_weight_square_total') or 0.0) + float(future['weight_square_total'])
    pass_total = float(state.get('raw_pass_weight_total') or 0.0) + float(future['weight_total'])
    wrong_total = float(state.get('raw_wrong_confident_weight_total') or 0.0)
    if total <= 0 or square_total <= 0:
        return {
            'weighted_success': 0.0,
            'effective_n': None,
            'rounded_effective_n': 0,
            'effective_successes': None,
            'lower95': None,
            'wrong_confident_rate': 0.0,
        }

    pass_rate = max(0.0, min(1.0, pass_total / total))
    effective_n = (total * total) / square_total
    rounded_effective_n = int(round(effective_n)) if effective_n else 0
    effective_successes = round(pass_rate * rounded_effective_n) if rounded_effective_n > 0 else None
    return {
        'weighted_success': pass_rate,
        'effective_n': effective_n,
        'rounded_effective_n': rounded_effective_n,
        'effective_successes': effective_successes,
        'lower95': (
            wilson_lower(int(effective_successes), rounded_effective_n)
            if effective_successes is not None and rounded_effective_n > 0
            else None
        ),
        'wrong_confident_rate': max(0.0, min(1.0, wrong_total / total)),
    }


def fixed_share_wrong_confident_rate(
    states: dict[str, dict[str, Any]],
    additions: dict[str, int],
) -> float | None:
    if not any('raw_weight_total' in state for state in states.values()):
        return None
    rate = 0.0
    for key, state in states.items():
        additional = int(additions.get(key, 0))
        if 'raw_weight_total' in state:
            stratum_rate = float(projected_raw_weight_stratum(state, additional)['wrong_confident_rate'] or 0.0)
        else:
            total = int(state.get('effective_n') or 0) + additional
            stratum_rate = 0.0 if total > 0 else 0.0
        rate += float(state.get('population_weight_share') or 0.0) * stratum_rate
    return max(0.0, min(1.0, rate))


def finite_projection_for_state(state: dict[str, Any], additional: int) -> dict[str, float | int | None]:
    if 'raw_weight_total' in state:
        return projected_raw_weight_stratum(state, additional)
    current_total = int(state.get('effective_n') or 0)
    current_successes = int(state.get('effective_successes') or 0)
    total = current_total + additional
    successes = current_successes + additional
    pass_rate = (successes / total) if total > 0 else 0.0
    return {
        'weighted_success': pass_rate,
        'effective_n': float(total),
        'rounded_effective_n': total,
        'effective_successes': successes,
        'lower95': wilson_lower(successes, total) if total > 0 else None,
        'wrong_confident_rate': 0.0,
    }


def upper_bound_projection_metrics(
    states: dict[str, dict[str, Any]],
    *,
    max_additions_by_stratum: dict[str, int] | None,
) -> dict[str, Any] | None:
    """Return the best possible all-pass projection under finite caps.

    Direct-provider strata are finite because the materializer samples from the
    frozen catalog/supportability-selectable rows.  Template-based
    multiround/ambiguity strata are currently uncapped, so their optimistic
    upper-bound contribution is one.  This prevents the planner from spending a
    long greedy loop on a target that cannot be reached under the scorer's
    weight model.
    """
    if not states:
        return None
    max_additions_by_stratum = max_additions_by_stratum or {}
    lower = 0.0
    observed = 0.0
    wrong = 0.0
    capped_additions: dict[str, int] = {}
    uncapped_strata: list[str] = []
    stratum_bounds: dict[str, dict[str, Any]] = {}
    for key, state in sorted(states.items()):
        share = float(state.get('population_weight_share') or 0.0)
        cap = max_additions_by_stratum.get(key)
        if cap is None:
            uncapped_strata.append(key)
            lower_i = observed_i = 1.0
            wrong_i = 0.0
            effective_n_i = None
            capped_additions[key] = 0
        else:
            capped_additions[key] = int(cap)
            projected = finite_projection_for_state(state, int(cap))
            lower_i = float(projected.get('lower95') or 0.0)
            observed_i = float(projected.get('weighted_success') or 0.0)
            wrong_i = float(projected.get('wrong_confident_rate') or 0.0)
            effective_n_i = projected.get('effective_n')
        lower += share * lower_i
        observed += share * observed_i
        wrong += share * wrong_i
        stratum_bounds[key] = {
            'population_weight_share': share,
            'finite_cap': cap,
            'optimistic_lower95': lower_i,
            'optimistic_observed_success': observed_i,
            'optimistic_wrong_confident_rate': wrong_i,
            'optimistic_effective_n': effective_n_i,
        }
    return {
        'lower95': max(0.0, min(1.0, lower)),
        'observed_success': max(0.0, min(1.0, observed)),
        'wrong_confident_answer_rate': max(0.0, min(1.0, wrong)),
        'finite_capped_additions': capped_additions,
        'uncapped_strata': uncapped_strata,
        'strata': stratum_bounds,
    }


def projection_meets_targets(
    *,
    lower95: float,
    observed_success: float,
    wrong_confident_answer_rate: float | None,
    target_lower95: float,
    target_observed_success: float,
    target_wrong_confident_rate: float | None,
) -> bool:
    if lower95 < target_lower95 or observed_success < target_observed_success:
        return False
    if (
        target_wrong_confident_rate is not None
        and wrong_confident_answer_rate is not None
        and wrong_confident_answer_rate > target_wrong_confident_rate
    ):
        return False
    return True


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


def build_raw_weight_design_states(
    *,
    score_report: dict[str, Any],
    design_strata: dict[str, Any],
    required_by_type: dict[str, dict[str, dict[str, Any]]],
    direct_provider_capacities: dict[str, dict[str, Any]],
    current_stats_by_type: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build projection states from the exact rows that the scorer used.

    ``build_design_states`` projects from rounded effective n only.  That is
    safe as a fallback, but it overstates large expansions when cumulative
    raw-selection weights are highly uneven across old and new batches.  This
    row-backed state keeps the planning projection aligned with
    ``score_certification.py`` without changing thresholds or evidence
    denominators.
    """
    required_keys = required_design_keys(required_by_type)
    rows_by_stratum: dict[str, list[dict[str, Any]]] = {key: [] for key in required_keys}
    for row in score_report.get('session_results') or []:
        key = str(row.get('design_stratum') or '')
        if key in rows_by_stratum:
            rows_by_stratum[key].append(row)

    states: dict[str, dict[str, Any]] = {}
    for dataset_type, group_policy in required_by_type.items():
        for name in group_policy:
            key = design_stratum_key(dataset_type, name)
            stratum = dict(design_strata.get(key) or {})
            rows = rows_by_stratum.get(key) or []
            raw_weights: list[float] = []
            pass_weights: list[float] = []
            wrong_weights: list[float] = []
            for row in rows:
                raw_weight = float(((row.get('provenance') or {}).get('selection_weight')) or 0.0)
                if raw_weight <= 0:
                    continue
                success = row.get('adjudicated_pass')
                if success is None:
                    success = row.get('provisional_structural_pass')
                raw_weights.append(raw_weight)
                if success is True:
                    pass_weights.append(raw_weight)
                if success is False and str(row.get('final_failure_class') or '') == 'wrong_confident_answer':
                    wrong_weights.append(raw_weight)

            if not raw_weights or not stratum:
                continue

            state: dict[str, Any] = {
                'dataset_type': dataset_type,
                'name': name,
                'population_weight_share': float(stratum.get('population_weight_share') or 0.0),
                'raw_weight_total': sum(raw_weights),
                'raw_weight_square_total': sum(weight * weight for weight in raw_weights),
                'raw_pass_weight_total': sum(pass_weights),
                'raw_wrong_confident_weight_total': sum(wrong_weights),
                'current_n': current_n_for_target(
                    current_stats=current_stats_by_type.get(dataset_type, {}),
                    design_strata=design_strata,
                    dataset_type=dataset_type,
                    name=name,
                ),
            }
            if dataset_type == 'direct':
                capacity = (
                    direct_provider_capacities.get(name)
                    or direct_provider_capacities.get(str(name).upper())
                    or direct_provider_capacities.get(str(name).lower())
                    or {}
                )
                state['future_weight_model'] = 'direct_catalog_population'
                state['future_population_count'] = int(
                    capacity.get('catalog_rows')
                    or stratum.get('weight_total')
                    or state['raw_weight_total']
                    or 0
                )
            else:
                state['future_weight_model'] = 'template_target_total'
                state['future_current_n'] = int(state['current_n'])
            states[key] = state
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
    target_wrong_confident_rate: float | None = None,
    max_additions_by_stratum: dict[str, int] | None = None,
    max_iterations: int = 200_000,
) -> tuple[dict[str, int], float, float, bool, str | None]:
    additions = {key: 0 for key in states}
    projected_lower = fixed_share_design_lower(states, additions)
    projected_observed = fixed_share_observed_success(states, additions)
    projected_wrong = fixed_share_wrong_confident_rate(states, additions)
    max_additions_by_stratum = max_additions_by_stratum or {}

    iterations = 0
    while (
        (
            projected_lower < target_lower95
            or projected_observed < target_observed_success
            or (
                target_wrong_confident_rate is not None
                and projected_wrong is not None
                and projected_wrong > target_wrong_confident_rate
            )
        )
        and iterations < max_iterations
    ):
        best_key = None
        best_gain = None
        current_components = [
            projected_lower / target_lower95 if target_lower95 > 0 else 1.0,
            projected_observed / target_observed_success if target_observed_success > 0 else 1.0,
        ]
        if target_wrong_confident_rate is not None and projected_wrong is not None:
            current_components.append(
                1.0
                if projected_wrong <= 0
                else target_wrong_confident_rate / projected_wrong
            )
        current_score = min(current_components)
        for key in states:
            cap = max_additions_by_stratum.get(key)
            if cap is not None and additions.get(key, 0) >= cap:
                continue
            trial = dict(additions)
            trial[key] += 1
            trial_lower = fixed_share_design_lower(states, trial)
            trial_observed = fixed_share_observed_success(states, trial)
            trial_wrong = fixed_share_wrong_confident_rate(states, trial)
            trial_components = [
                trial_lower / target_lower95 if target_lower95 > 0 else 1.0,
                trial_observed / target_observed_success if target_observed_success > 0 else 1.0,
            ]
            if target_wrong_confident_rate is not None and trial_wrong is not None:
                trial_components.append(
                    1.0
                    if trial_wrong <= 0
                    else target_wrong_confident_rate / trial_wrong
                )
            trial_score = min(trial_components)
            lower_gain = trial_lower - projected_lower
            observed_gain = trial_observed - projected_observed
            wrong_gain = (projected_wrong or 0.0) - (trial_wrong or 0.0)
            gain = (trial_score - current_score, lower_gain, observed_gain, wrong_gain, states[key]['population_weight_share'])
            if best_gain is None or gain > best_gain:
                best_gain = gain
                best_key = key
        if best_key is None:
            return additions, projected_lower, projected_observed, False, 'all_stratum_capacity_caps_exhausted'
        additions[best_key] += 1
        projected_lower = fixed_share_design_lower(states, additions)
        projected_observed = fixed_share_observed_success(states, additions)
        projected_wrong = fixed_share_wrong_confident_rate(states, additions)
        iterations += 1

    if (
        projected_lower < target_lower95
        or projected_observed < target_observed_success
        or (
            target_wrong_confident_rate is not None
            and projected_wrong is not None
            and projected_wrong > target_wrong_confident_rate
        )
    ):
        if iterations >= max_iterations:
            reason = f'max_iterations_{max_iterations}_exhausted'
        else:
            reason = 'projected_threshold_not_reached'
        return additions, projected_lower, projected_observed, False, reason

    return additions, projected_lower, projected_observed, True, None


def load_direct_provider_capacities(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        '''
        SELECT provider, code, name, category
        FROM indicators
        ORDER BY provider, id
        '''
    ).fetchall()
    con.close()

    capacities: dict[str, dict[str, Any]] = {}
    for row in rows:
        provider = str(row['provider'] or '')
        item = capacities.setdefault(
            provider,
            {
                'catalog_rows': 0,
                'answerability_selectable_rows': 0,
                'supportability_excluded_rows': 0,
                'supportability_exclusion_reasons': {},
            },
        )
        item['catalog_rows'] += 1
        reason = None
        if provider.upper() == 'IMF':
            reason = imf_catalog_sampler_supportability_reason(
                str(row['code'] or ''),
                str(row['name'] or ''),
                str(row['category'] or ''),
            )
        if reason:
            item['supportability_excluded_rows'] += 1
            reasons = item['supportability_exclusion_reasons']
            reasons[reason] = int(reasons.get(reason, 0)) + 1
        else:
            item['answerability_selectable_rows'] += 1
    return capacities


def direct_capacity_addition_caps(
    *,
    capacities: dict[str, dict[str, Any]],
    direct_policy: dict[str, dict[str, Any]],
    current_stats: dict[str, dict[str, Any]],
    design_strata: dict[str, Any],
) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    caps: dict[str, int] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for provider in direct_policy:
        capacity = capacities.get(provider) or capacities.get(provider.upper()) or capacities.get(provider.lower())
        if not capacity:
            continue
        current_n = current_n_for_target(
            current_stats=current_stats,
            design_strata=design_strata,
            dataset_type='direct',
            name=provider,
        )
        selectable = int(capacity.get('answerability_selectable_rows') or 0)
        max_additional = max(0, selectable - current_n)
        caps[design_stratum_key('direct', provider)] = max_additional
        metadata[provider] = {
            **capacity,
            'current_n': current_n,
            'max_additional_answerability_sessions': max_additional,
        }
    return caps, metadata


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
    parser.add_argument('--db-path', type=Path, default=DEFAULT_DB)
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
    target_wrong_confident_rate = (
        float(claim_thresholds['wrong_confident_answer_rate_max'])
        if claim_thresholds.get('wrong_confident_answer_rate_max') is not None
        else None
    )

    design_conf = design_confidence(score_report)
    design_strata = dict(design_conf.get('strata') or {})
    current_stats_by_type = {
        'direct': dict(strata.get('evaluated_provider_floors') or {}),
        'multiround': dict(strata.get('evaluated_multiround_family_floors') or {}),
        'ambiguity': dict(strata.get('evaluated_ambiguity_family_floors') or {}),
    }
    provider_capacities: dict[str, dict[str, Any]] = {}
    if direct_policy:
        provider_capacities = load_direct_provider_capacities(args.db_path.resolve())
    raw_design_states = build_raw_weight_design_states(
        score_report=score_report,
        design_strata=design_strata,
        required_by_type=required_by_type,
        direct_provider_capacities=provider_capacities,
        current_stats_by_type=current_stats_by_type,
    )
    design_states = build_design_states(
        design_strata=design_strata,
        required_by_type=required_by_type,
    )
    projection_states = raw_design_states or design_states
    projection_method = (
        'greedy_fixed-share_raw-selection-weight_kish_projection'
        if raw_design_states
        else 'greedy_fixed-share_per_stratum_wilson_projection'
    )
    missing_design_keys = sorted(required_design_keys(required_by_type) - set(projection_states))
    design_plan_metadata: dict[str, Any] | None = None
    capacity_metadata: dict[str, Any] = {}
    max_additions_by_stratum: dict[str, int] = {}
    if direct_policy:
        max_additions_by_stratum, capacity_metadata = direct_capacity_addition_caps(
            capacities=provider_capacities,
            direct_policy=direct_policy,
            current_stats=current_stats_by_type['direct'],
            design_strata=design_strata,
        )

    if projection_states and not missing_design_keys:
        upper_bound = upper_bound_projection_metrics(
            projection_states,
            max_additions_by_stratum=max_additions_by_stratum,
        )
        upper_bound_reaches_target = (
            projection_meets_targets(
                lower95=float((upper_bound or {}).get('lower95') or 0.0),
                observed_success=float((upper_bound or {}).get('observed_success') or 0.0),
                wrong_confident_answer_rate=(upper_bound or {}).get('wrong_confident_answer_rate'),
                target_lower95=target_lower95,
                target_observed_success=target_observed_success,
                target_wrong_confident_rate=target_wrong_confident_rate,
            )
            if upper_bound is not None
            else True
        )
        if upper_bound is not None and not upper_bound_reaches_target:
            additions_by_stratum = {key: 0 for key in projection_states}
            projected_lower95 = fixed_share_design_lower(projection_states, additions_by_stratum)
            projected_observed = fixed_share_observed_success(projection_states, additions_by_stratum)
            target_feasible = False
            infeasible_reason = 'target_not_reachable_under_weight_capacity_upper_bound'
        else:
            (
                additions_by_stratum,
                projected_lower95,
                projected_observed,
                target_feasible,
                infeasible_reason,
            ) = greedy_design_additions(
                projection_states,
                target_lower95=target_lower95,
                target_observed_success=target_observed_success,
                target_wrong_confident_rate=target_wrong_confident_rate,
                max_additions_by_stratum=max_additions_by_stratum,
            )
            upper_bound = upper_bound or upper_bound_projection_metrics(
                projection_states,
                max_additions_by_stratum=max_additions_by_stratum,
            )
        projected_wrong_confident = fixed_share_wrong_confident_rate(projection_states, additions_by_stratum)
        additional_needed = sum(additions_by_stratum.values())
        direct_plan = allocate_design_type_plan(
            dataset_type='direct',
            group_policy=direct_policy,
            current_stats=current_stats_by_type['direct'],
            design_strata=design_strata,
            additions_by_stratum=additions_by_stratum,
        )
        multiround_plan = allocate_design_type_plan(
            dataset_type='multiround',
            group_policy=multiround_policy,
            current_stats=current_stats_by_type['multiround'],
            design_strata=design_strata,
            additions_by_stratum=additions_by_stratum,
        )
        ambiguity_plan = allocate_design_type_plan(
            dataset_type='ambiguity',
            group_policy=ambiguity_policy,
            current_stats=current_stats_by_type['ambiguity'],
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
            'method': projection_method,
            'current_observed_success': design_conf.get('observed_success'),
            'current_lower95': design_conf.get('lower95'),
            'current_wrong_confident_answer_rate': (score_report.get('metrics') or {}).get('wrong_confident_answer_rate'),
            'target_observed_success': target_observed_success,
            'target_lower95': target_lower95,
            'target_wrong_confident_answer_rate': target_wrong_confident_rate,
            'projected_observed_success_after_plan': projected_observed,
            'projected_lower95_after_plan': projected_lower95,
            'projected_wrong_confident_answer_rate_after_plan': projected_wrong_confident,
            'optimistic_upper_bound_under_caps': upper_bound,
            'additional_target_sessions': additional_needed,
            'target_feasible_under_capacity_caps': target_feasible,
            'infeasible_reason': infeasible_reason,
            'capacity_caps_by_stratum': max_additions_by_stratum,
            'direct_provider_answerability_capacity': capacity_metadata,
            'missing_required_design_strata': [],
            'caveat': (
                'Projection assumes the added review sessions pass and preserve fixed design-stratum '
                'population shares. When session_results are available it mirrors score_certification.py '
                'by projecting Kish effective n from raw selection weights instead of treating each added '
                'session as +1 effective n. Direct-provider additions are capped by provider-native '
                'answerability/supportability capacity; the next score report remains authoritative.'
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
