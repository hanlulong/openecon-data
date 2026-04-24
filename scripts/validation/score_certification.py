#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'certification-score-summary.json'
DEFAULT_FLOOR_POLICY = ROOT / 'validation' / 'manifests' / 'claim_gate_policy-v1.json'


def iter_jsonl(path: Path):
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def dataset_type(row: dict[str, Any]) -> str:
    if 'rounds' in row:
        return 'multiround'
    if 'expected_behavior' in row:
        return 'ambiguity'
    return 'direct'


def structural_pass(result: dict[str, Any]) -> bool:
    return int(result.get('status_code') or 0) == 200 and not result.get('error') and int(result.get('series_count') or 0) > 0


def clarification_path_pass(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(
        int(row.get('status_code') or 0) == 200 and not row.get('error')
        for row in rows
    )


def kish_effective_n(weights: list[float]) -> float | None:
    if not weights:
        return None
    total = sum(weights)
    denom = sum(w * w for w in weights)
    if total <= 0 or denom <= 0:
        return None
    return (total * total) / denom


def design_stratum_for_session(session: dict[str, Any], kind: str) -> str:
    """Return the primary certification-design stratum for a session.

    The 30K certification surface is intentionally stratified across direct
    providers plus multiround/ambiguity families.  Confidence reporting should
    therefore preserve those strata instead of collapsing immediately to one
    pooled Kish effective-n approximation.
    """
    if kind == 'direct':
        provider = str(session.get('provider_stratum') or (session.get('origin') or {}).get('source_provider') or '<missing>')
        return f'direct_provider:{provider}'
    family = family_for_session(session, kind)
    if kind == 'multiround':
        return f'multiround_family:{family or "<missing>"}'
    if kind == 'ambiguity':
        return f'ambiguity_family:{family or "<missing>"}'
    return f'{kind}:<missing>'


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5) / denom
    return center - margin


def design_aware_weighted_confidence(
    records: list[dict[str, Any]],
    *,
    success_key: str,
    z: float = 1.96,
) -> dict[str, Any] | None:
    """Compute a conservative stratified lower bound for weighted success.

    This intentionally supersedes the older single pooled Kish approximation
    for claim gating.  Each design stratum receives its own weighted pass-rate
    estimate and Wilson lower bound using that stratum's Kish effective n; the
    overall lower bound is the population-weighted sum of stratum lower bounds.

    This is still a bounded, auditable estimator rather than a full survey
    statistics package, but it is design-aware in the ways that matter for this
    certification lane: weak providers/families remain visible, small strata are
    penalized instead of averaged away, and missing/unreviewed outcomes do not
    contribute to the claim-bound path.
    """
    eligible = [
        row
        for row in records
        if row.get(success_key) is not None
        and float(((row.get('provenance') or {}).get('selection_weight')) or 0.0) > 0
    ]
    if not eligible:
        return None

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        grouped[str(row.get('design_stratum') or '<missing>')].append(row)

    total_weight = sum(float(((row.get('provenance') or {}).get('selection_weight')) or 0.0) for row in eligible)
    if total_weight <= 0:
        return None

    overall_success = 0.0
    lower95 = 0.0
    stratum_reports: dict[str, dict[str, Any]] = {}
    for stratum, rows in sorted(grouped.items()):
        weights = [float(((row.get('provenance') or {}).get('selection_weight')) or 0.0) for row in rows]
        stratum_weight = sum(weights)
        if stratum_weight <= 0:
            continue
        pass_weight = sum(
            float(((row.get('provenance') or {}).get('selection_weight')) or 0.0)
            for row in rows
            if row.get(success_key) is True
        )
        pass_rate = pass_weight / stratum_weight
        effective_n = kish_effective_n(weights)
        rounded_effective_n = int(round(effective_n)) if effective_n else 0
        successes_effective = round(pass_rate * rounded_effective_n) if rounded_effective_n > 0 else None
        stratum_lower = (
            wilson_lower(int(successes_effective), rounded_effective_n, z=z)
            if successes_effective is not None and rounded_effective_n > 0
            else None
        )
        population_share = stratum_weight / total_weight
        overall_success += population_share * pass_rate
        if stratum_lower is not None:
            lower95 += population_share * stratum_lower
        stratum_reports[stratum] = {
            'n': len(rows),
            'weight_total': stratum_weight,
            'population_weight_share': population_share,
            'weighted_success': pass_rate,
            'effective_n': effective_n,
            'rounded_effective_n': rounded_effective_n,
            'effective_successes': successes_effective,
            'lower95': stratum_lower,
        }

    nominal_n = len(eligible)
    effective_n_total = sum(
        float(report.get('effective_n') or 0.0)
        for report in stratum_reports.values()
    )
    return {
        'method': 'stratified_weighted_wilson_by_design_stratum',
        'description': (
            'Population-weighted sum of per-design-stratum Wilson lower bounds; '
            'each stratum uses Kish effective n from selection weights.'
        ),
        'confidence_level': 0.95,
        'z': z,
        'success_key': success_key,
        'observed_success': overall_success,
        'lower95': max(0.0, min(1.0, lower95)),
        'nominal_n': nominal_n,
        'effective_n': effective_n_total,
        'design_effect': (nominal_n / effective_n_total) if effective_n_total > 0 else None,
        'strata_count': len(stratum_reports),
        'strata': stratum_reports,
    }


def ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(iter_jsonl(path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def label_is_success(label: Any) -> bool | None:
    if label is None:
        return None
    normalized = str(label).strip().lower()
    if normalized in {'pass', 'passed', 'correct', 'accepted', 'success'}:
        return True
    if normalized:
        return False
    return None


def canonical_failure_class(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized.endswith('_proxy'):
        return None
    return normalized


def family_for_session(session: dict[str, Any], kind: str) -> str | None:
    if kind == 'multiround':
        family = str(session.get('family') or '').strip()
        return family or None
    if kind == 'ambiguity':
        family = str(((session.get('provenance') or {}).get('family')) or '').strip()
        return family or None
    return None


def expected_clarification_for_session(session: dict[str, Any], kind: str) -> bool | None:
    if kind == 'direct':
        gold = session.get('gold') or {}
        if gold.get('clarification_expected') is True:
            return True
        return False
    if kind == 'ambiguity':
        behavior = str(session.get('expected_behavior') or '').strip().lower()
        if behavior == 'clarify':
            return True
        if behavior == 'direct_answer':
            return False
    return None


def adjudicated_replay_conflict_reason(
    *,
    kind: str,
    adjudicated_pass: bool | None,
    all_pass: bool,
    expected_clarification: bool | None,
    session_clarification_detected: bool,
    session_answer_present: bool,
) -> str | None:
    if adjudicated_pass is not True:
        return None
    if kind == 'multiround' and not all_pass:
        return 'adjudicated pass conflicts with multiround replay (one or more turns failed structural replay)'
    if expected_clarification is True and not session_clarification_detected:
        return 'adjudicated pass expected clarification but replay did not clarify'
    if expected_clarification is False:
        if session_clarification_detected:
            return 'adjudicated pass expected direct answer but replay asked for clarification'
        if not session_answer_present:
            return 'adjudicated pass expected direct answer but replay returned no answer'
    return None


def evaluate_required_floors(
    stats_map: dict[str, dict[str, Any]],
    required_map: dict[str, Any],
    *,
    label: str,
    failing_strata: list[str],
    missing_required_strata: list[str],
) -> dict[str, Any]:
    evaluated: dict[str, Any] = {}
    for key, policy in required_map.items():
        floor = float(policy['floor'])
        policy_class = str(policy.get('class') or '<missing>')
        stats = stats_map.get(key)
        if stats is None:
            missing_required_strata.append(f'{label}:{key} ({policy_class})')
            evaluated[key] = {
                'class': policy_class,
                'floor': floor,
                'n': 0,
                'pass_rate': None,
                'status': 'missing',
            }
            continue
        pass_rate = stats['pass_rate']
        status = 'pass'
        if pass_rate is None or pass_rate < floor:
            status = 'fail'
            rendered_rate = 'None' if pass_rate is None else f'{pass_rate:.3f}'
            failing_strata.append(f'{label}:{key} {rendered_rate} below {policy_class} floor {floor:.3f}')
        evaluated[key] = {
            'class': policy_class,
            'floor': floor,
            'n': stats['n'],
            'pass_rate': pass_rate,
            'status': status,
        }
    return evaluated


def main() -> int:
    parser = argparse.ArgumentParser(description='Score raw certification execution results with a provisional structural scorer.')
    parser.add_argument('--dataset', action='append', type=Path, required=True)
    parser.add_argument('--raw-results', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--floor-policy', type=Path, default=DEFAULT_FLOOR_POLICY)
    parser.add_argument('--adjudication-records', type=Path, default=None)
    parser.add_argument('--max-sessions', type=int, default=None)
    args = parser.parse_args()

    sessions: dict[str, dict[str, Any]] = {}
    session_order: list[str] = []
    floor_policy = load_json(args.floor_policy.resolve())
    adjudication_records: dict[str, dict[str, Any]] = {}
    adjudication_path = args.adjudication_records.resolve() if args.adjudication_records is not None else None
    if adjudication_path is not None and adjudication_path.exists():
        for row in load_jsonl(adjudication_path):
            adjudication_records[str(row.get('session_id') or '')] = row
    dataset_inputs: list[dict[str, Any]] = []
    for dataset in args.dataset:
        dataset_path = dataset.resolve()
        dataset_rows = list(iter_jsonl(dataset_path))
        dataset_inputs.append(
            {
                'path': str(dataset_path),
                'sha256': sha256_file(dataset_path),
                'row_count': len(dataset_rows),
            }
        )
        for row in dataset_rows:
            sid = str(row.get('id') or '')
            if sid not in sessions:
                session_order.append(sid)
            sessions[sid] = row
    if args.max_sessions is not None:
        allowed_ids = set(session_order[:args.max_sessions])
        sessions = {sid: sessions[sid] for sid in session_order if sid in allowed_ids}

    raw_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in iter_jsonl(args.raw_results.resolve()):
        raw_by_session[str(row.get('session_id') or '')].append(row)

    session_results = []
    counts_by_type = Counter()
    counts_by_tier = Counter()
    counts_by_split = Counter()
    provisional_pass_by_type = Counter()
    provisional_pass_by_split = Counter()
    adjudicated_pass_by_type = Counter()
    adjudicated_pass_by_split = Counter()
    direct_provider_counts = Counter()
    direct_provider_pass_counts = Counter()
    direct_provider_adjudicated_pass_counts = Counter()
    multiround_family_counts = Counter()
    multiround_family_pass_counts = Counter()
    multiround_family_adjudicated_pass_counts = Counter()
    ambiguity_family_counts = Counter()
    ambiguity_family_pass_counts = Counter()
    ambiguity_family_adjudicated_pass_counts = Counter()
    weighted_totals_by_type: dict[str, float] = defaultdict(float)
    weighted_pass_totals_by_type: dict[str, float] = defaultdict(float)
    adjudicated_weighted_pass_totals_by_type: dict[str, float] = defaultdict(float)
    weighted_session_counts_by_type = Counter()
    direct_weights: list[float] = []
    direct_pass_weights: list[float] = []
    all_weights: list[float] = []
    all_pass_weights: list[float] = []
    all_adjudicated_pass_weights: list[float] = []
    all_reviewed_weights: list[float] = []
    snapshot_ids: set[str] = set()
    adjudicated_records_total = 0
    expected_no_clarification_total = 0
    expected_no_clarification_with_unnecessary = 0
    expected_clarification_total = 0
    expected_clarification_detected_total = 0
    ambiguity_resolution_adjudicated_total = 0
    ambiguity_resolution_adjudicated_success_total = 0
    wrong_confident_total = 0
    expected_no_clarification_weight_total = 0.0
    expected_no_clarification_unnecessary_weight = 0.0
    expected_clarification_weight_total = 0.0
    expected_clarification_success_weight = 0.0
    wrong_confident_weight_total = 0.0
    reviewed_failure_class_missing = 0
    adjudicated_replay_conflicts: list[str] = []

    for sid, session in sessions.items():
        kind = dataset_type(session)
        tier = str(session.get('dataset_tier') or '<missing>')
        split = str((session.get('provenance') or {}).get('holdout_split') or '<missing>')
        provider = str(session.get('provider_stratum') or (session.get('origin') or {}).get('source_provider') or '<missing>')
        family = family_for_session(session, kind)
        rows = sorted(raw_by_session.get(sid, []), key=lambda r: int(r.get('round_index') or 0))
        session_clarification_detected = any(bool(row.get('clarification_detected')) for row in rows)
        session_answer_present = any(
            (int(row.get('series_count') or 0) > 0 or bool(row.get('response_text_present')))
            and not bool(row.get('clarification_detected'))
            for row in rows
        )
        expected_clarification = expected_clarification_for_session(session, kind)
        design_stratum = design_stratum_for_session(session, kind)
        all_pass = bool(rows) and all(structural_pass(r) for r in rows)
        provisional_pass = (
            clarification_path_pass(rows) and session_clarification_detected
            if expected_clarification is True
            else all_pass
        )
        any_error = next((r.get('error') for r in rows if r.get('error')), None)
        snapshot_id = str((session.get('provenance') or {}).get('snapshot_id') or '').strip()
        if snapshot_id:
            snapshot_ids.add(snapshot_id)
        adjudication_row = adjudication_records.get(sid)
        final_label = adjudication_row.get('final_label') if adjudication_row else None
        adjudicated_pass = label_is_success(final_label)
        final_failure_class = canonical_failure_class(adjudication_row.get('failure_class')) if adjudication_row else None
        if adjudicated_pass is not None:
            adjudicated_records_total += 1
        if expected_clarification is False:
            expected_no_clarification_total += 1
            if session_clarification_detected:
                expected_no_clarification_with_unnecessary += 1
        elif expected_clarification is True:
            expected_clarification_total += 1
            if session_clarification_detected:
                expected_clarification_detected_total += 1
            if kind == 'ambiguity' and adjudicated_pass is not None:
                ambiguity_resolution_adjudicated_total += 1
                if session_clarification_detected and adjudicated_pass:
                    ambiguity_resolution_adjudicated_success_total += 1
        if adjudicated_pass is False and not session_clarification_detected and session_answer_present:
            wrong_confident_total += 1
        replay_conflict = adjudicated_replay_conflict_reason(
            kind=kind,
            adjudicated_pass=adjudicated_pass,
            all_pass=all_pass,
            expected_clarification=expected_clarification,
            session_clarification_detected=session_clarification_detected,
            session_answer_present=session_answer_present,
        )
        if replay_conflict is not None:
            adjudicated_replay_conflicts.append(f'{sid}: {replay_conflict}')
        result_record = {
            'session_id': sid,
            'dataset_type': kind,
            'dataset_tier': tier,
            'holdout_split': split,
            'provider_stratum': provider,
            'family_stratum': family,
            'design_stratum': design_stratum,
            'provisional_structural_pass': provisional_pass,
            'final_label': final_label,
            'final_failure_class': final_failure_class,
            'adjudicated_pass': adjudicated_pass,
            'expected_clarification': expected_clarification,
            'clarification_detected': session_clarification_detected,
            'answer_present_without_clarification': session_answer_present,
            'adjudicated_replay_conflict': replay_conflict,
            'round_count_expected': len(session.get('rounds', [])) if kind == 'multiround' else 1,
            'round_count_observed': len(rows),
            'error': any_error,
            'human_review_required': bool((session.get('gold') or {}).get('human_review_required') or any((round_case.get('gold') or {}).get('human_review_required') for round_case in session.get('rounds', []))),
            'provenance': session.get('provenance'),
        }
        session_results.append(result_record)
        counts_by_type[kind] += 1
        counts_by_tier[tier] += 1
        counts_by_split[split] += 1
        if provisional_pass:
            provisional_pass_by_type[kind] += 1
            provisional_pass_by_split[split] += 1
        if adjudicated_pass:
            adjudicated_pass_by_type[kind] += 1
            adjudicated_pass_by_split[split] += 1

        weight = float(((session.get('provenance') or {}).get('selection_weight')) or 0.0)
        if weight > 0:
            weighted_totals_by_type[kind] += weight
            weighted_session_counts_by_type[kind] += 1
            all_weights.append(weight)
            if provisional_pass:
                weighted_pass_totals_by_type[kind] += weight
                all_pass_weights.append(weight)
            if adjudicated_pass is not None:
                all_reviewed_weights.append(weight)
            if adjudicated_pass:
                adjudicated_weighted_pass_totals_by_type[kind] += weight
                all_adjudicated_pass_weights.append(weight)
            if expected_clarification is False:
                expected_no_clarification_weight_total += weight
                if final_failure_class == 'unnecessary_clarification':
                    expected_no_clarification_unnecessary_weight += weight
            elif expected_clarification is True:
                expected_clarification_weight_total += weight
                if adjudicated_pass:
                    expected_clarification_success_weight += weight
            if adjudicated_pass is False and final_failure_class == 'wrong_confident_answer':
                wrong_confident_weight_total += weight
        if kind == 'direct':
            direct_provider_counts[provider] += 1
            if all_pass:
                direct_provider_pass_counts[provider] += 1
            if adjudicated_pass:
                direct_provider_adjudicated_pass_counts[provider] += 1
            if weight > 0:
                direct_weights.append(weight)
                if provisional_pass:
                    direct_pass_weights.append(weight)
        elif kind == 'multiround' and family:
            multiround_family_counts[family] += 1
            if provisional_pass:
                multiround_family_pass_counts[family] += 1
            if adjudicated_pass:
                multiround_family_adjudicated_pass_counts[family] += 1
        elif kind == 'ambiguity' and family:
            ambiguity_family_counts[family] += 1
            if provisional_pass:
                ambiguity_family_pass_counts[family] += 1
            if adjudicated_pass:
                ambiguity_family_adjudicated_pass_counts[family] += 1

    direct_weight_total = sum(direct_weights)
    direct_weight_pass = sum(direct_pass_weights)
    direct_weighted_success = (direct_weight_pass / direct_weight_total) if direct_weight_total else None
    direct_effective_n = kish_effective_n(direct_weights)
    direct_unweighted_successes = sum(1 for r in session_results if r['dataset_type'] == 'direct' and r['provisional_structural_pass'])
    direct_unweighted_total = sum(1 for r in session_results if r['dataset_type'] == 'direct')
    direct_lower95_unweighted = wilson_lower(direct_unweighted_successes, direct_unweighted_total)
    direct_weighted_successes_approx = round((direct_weighted_success or 0.0) * direct_effective_n) if direct_effective_n else None
    direct_lower95_effective_n = wilson_lower(int(direct_weighted_successes_approx), int(round(direct_effective_n))) if direct_effective_n and direct_weighted_successes_approx is not None else None
    overall_weight_total = sum(all_weights)
    overall_weight_pass = sum(all_pass_weights)
    overall_weighted_success = ratio(overall_weight_pass, overall_weight_total)
    overall_effective_n = kish_effective_n(all_weights)
    overall_weighted_successes_approx = round((overall_weighted_success or 0.0) * overall_effective_n) if overall_effective_n else None
    overall_weighted_lower95 = wilson_lower(int(overall_weighted_successes_approx), int(round(overall_effective_n))) if overall_effective_n and overall_weighted_successes_approx is not None else None
    overall_reviewed_weight_total = sum(all_reviewed_weights)
    overall_adjudication_weight_coverage = ratio(overall_reviewed_weight_total, overall_weight_total)
    overall_adjudicated_weight_pass = sum(all_adjudicated_pass_weights)
    overall_adjudicated_weighted_success = ratio(overall_adjudicated_weight_pass, overall_weight_total)
    overall_adjudicated_weighted_successes_approx = round((overall_adjudicated_weighted_success or 0.0) * overall_effective_n) if overall_effective_n and overall_adjudication_weight_coverage == 1.0 else None
    overall_adjudicated_weighted_lower95 = wilson_lower(int(overall_adjudicated_weighted_successes_approx), int(round(overall_effective_n))) if overall_effective_n and overall_adjudicated_weighted_successes_approx is not None else None
    overall_design_confidence = design_aware_weighted_confidence(
        session_results,
        success_key='provisional_structural_pass',
    )
    overall_adjudicated_design_confidence = (
        design_aware_weighted_confidence(
            session_results,
            success_key='adjudicated_pass',
        )
        if overall_adjudication_weight_coverage == 1.0
        else None
    )
    claim_metric_source = 'adjudicated_structural' if overall_adjudication_weight_coverage == 1.0 and overall_adjudicated_weighted_success is not None else None
    claim_observed_success = overall_adjudicated_weighted_success if claim_metric_source else None
    claim_confidence_method = (
        overall_adjudicated_design_confidence.get('method')
        if claim_metric_source and overall_adjudicated_design_confidence
        else None
    )
    claim_lower95 = (
        overall_adjudicated_design_confidence.get('lower95')
        if claim_metric_source and overall_adjudicated_design_confidence
        else None
    )
    wrong_confident_answer_rate = None
    unnecessary_clarification_rate = None
    ambiguity_resolution_success = None
    if overall_adjudication_weight_coverage == 1.0:
        reviewed_failure_class_missing = sum(
            1
            for row in session_results
            if row['adjudicated_pass'] is False and row['final_failure_class'] is None
        )
        if reviewed_failure_class_missing == 0:
            wrong_confident_answer_rate = ratio(wrong_confident_weight_total, overall_reviewed_weight_total)
            unnecessary_clarification_rate = ratio(
                expected_no_clarification_unnecessary_weight,
                expected_no_clarification_weight_total,
            )
            ambiguity_resolution_success = ratio(
                expected_clarification_success_weight,
                expected_clarification_weight_total,
            )
    direct_provider_success = {
        provider: {
            'n': direct_provider_counts[provider],
            'passed': direct_provider_pass_counts[provider],
            'pass_rate': ratio(direct_provider_pass_counts[provider], direct_provider_counts[provider]),
        }
        for provider in sorted(direct_provider_counts)
    }
    direct_provider_adjudicated_success = {
        provider: {
            'n': direct_provider_counts[provider],
            'passed': direct_provider_adjudicated_pass_counts[provider],
            'pass_rate': ratio(direct_provider_adjudicated_pass_counts[provider], direct_provider_counts[provider]),
        }
        for provider in sorted(direct_provider_counts)
    }
    multiround_family_success = {
        family: {
            'n': multiround_family_counts[family],
            'passed': multiround_family_pass_counts[family],
            'pass_rate': ratio(multiround_family_pass_counts[family], multiround_family_counts[family]),
        }
        for family in sorted(multiround_family_counts)
    }
    multiround_family_adjudicated_success = {
        family: {
            'n': multiround_family_counts[family],
            'passed': multiround_family_adjudicated_pass_counts[family],
            'pass_rate': ratio(multiround_family_adjudicated_pass_counts[family], multiround_family_counts[family]),
        }
        for family in sorted(multiround_family_counts)
    }
    ambiguity_family_success = {
        family: {
            'n': ambiguity_family_counts[family],
            'passed': ambiguity_family_pass_counts[family],
            'pass_rate': ratio(ambiguity_family_pass_counts[family], ambiguity_family_counts[family]),
        }
        for family in sorted(ambiguity_family_counts)
    }
    ambiguity_family_adjudicated_success = {
        family: {
            'n': ambiguity_family_counts[family],
            'passed': ambiguity_family_adjudicated_pass_counts[family],
            'pass_rate': ratio(ambiguity_family_adjudicated_pass_counts[family], ambiguity_family_counts[family]),
        }
        for family in sorted(ambiguity_family_counts)
    }
    required_direct_provider_floors = dict((floor_policy or {}).get('required_direct_provider_floors') or {})
    required_multiround_family_floors = dict((floor_policy or {}).get('required_multiround_family_floors') or {})
    required_ambiguity_family_floors = dict((floor_policy or {}).get('required_ambiguity_family_floors') or {})
    failing_strata: list[str] = []
    missing_required_strata: list[str] = []
    floor_metric_source = 'adjudicated' if overall_adjudication_weight_coverage == 1.0 else 'provisional'
    provider_floor_stats = direct_provider_adjudicated_success if floor_metric_source == 'adjudicated' else direct_provider_success
    multiround_floor_stats = multiround_family_adjudicated_success if floor_metric_source == 'adjudicated' else multiround_family_success
    ambiguity_floor_stats = ambiguity_family_adjudicated_success if floor_metric_source == 'adjudicated' else ambiguity_family_success
    evaluated_provider_floors = evaluate_required_floors(
        provider_floor_stats,
        required_direct_provider_floors,
        label='direct_provider',
        failing_strata=failing_strata,
        missing_required_strata=missing_required_strata,
    )
    evaluated_multiround_family_floors = evaluate_required_floors(
        multiround_floor_stats,
        required_multiround_family_floors,
        label='multiround_family',
        failing_strata=failing_strata,
        missing_required_strata=missing_required_strata,
    )
    evaluated_ambiguity_family_floors = evaluate_required_floors(
        ambiguity_floor_stats,
        required_ambiguity_family_floors,
        label='ambiguity_family',
        failing_strata=failing_strata,
        missing_required_strata=missing_required_strata,
    )

    metrics = {
        'provisional_structural_session_success': {
            'overall_unweighted': ratio(sum(1 for r in session_results if r['provisional_structural_pass']), len(session_results)) or 0.0,
            'by_type': {
                kind: ratio(provisional_pass_by_type[kind], counts_by_type[kind]) or 0.0
                for kind in counts_by_type
            },
            'by_tier': {
                tier: ratio(
                    sum(1 for r in session_results if r['dataset_tier'] == tier and r['provisional_structural_pass']),
                    counts_by_tier[tier],
                ) or 0.0
                for tier in counts_by_tier
            },
            'by_split': {
                split: ratio(provisional_pass_by_split[split], counts_by_split[split]) or 0.0
                for split in counts_by_split
            },
        },
        'adjudicated_session_success': {
            'overall_unweighted': ratio(sum(1 for r in session_results if r['adjudicated_pass'] is True), adjudicated_records_total) if adjudicated_records_total else None,
            'by_type': {
                kind: ratio(adjudicated_pass_by_type[kind], sum(1 for r in session_results if r['dataset_type'] == kind and r['adjudicated_pass'] is not None))
                for kind in counts_by_type
            },
            'by_split': {
                split: ratio(adjudicated_pass_by_split[split], sum(1 for r in session_results if r['holdout_split'] == split and r['adjudicated_pass'] is not None))
                for split in counts_by_split
            },
        },
        'direct_weighted_provisional_success': direct_weighted_success,
        'direct_weighted_effective_n': direct_effective_n,
        'direct_unweighted_lower95': direct_lower95_unweighted,
        'direct_weighted_lower95_approx': direct_lower95_effective_n,
        'overall_weighted_provisional_success': overall_weighted_success,
        'overall_weighted_effective_n': overall_effective_n,
        'overall_weighted_lower95_approx': overall_weighted_lower95,
        'overall_weighted_design_confidence': overall_design_confidence,
        'overall_adjudication_weight_coverage': overall_adjudication_weight_coverage,
        'overall_weighted_adjudicated_success': overall_adjudicated_weighted_success,
        'overall_weighted_adjudicated_lower95_approx': overall_adjudicated_weighted_lower95,
        'overall_weighted_adjudicated_design_confidence': overall_adjudicated_design_confidence,
        'claim_metric_source': claim_metric_source,
        'claim_confidence_method': claim_confidence_method,
        'claim_observed_success': claim_observed_success,
        'claim_lower95': claim_lower95,
        'weighted_by_type': {
            kind: ratio(weighted_pass_totals_by_type[kind], weighted_totals_by_type[kind])
            for kind in sorted(weighted_totals_by_type)
        },
        'weighted_adjudicated_by_type': {
            kind: ratio(adjudicated_weighted_pass_totals_by_type[kind], weighted_totals_by_type[kind])
            for kind in sorted(weighted_totals_by_type)
        },
        'weighted_session_counts_by_type': dict(weighted_session_counts_by_type),
        'adjudicated_replay_conflict_count': len(adjudicated_replay_conflicts),
        'wrong_confident_answer_rate_proxy': ratio(wrong_confident_total, adjudicated_records_total),
        'unnecessary_clarification_rate_proxy': ratio(expected_no_clarification_with_unnecessary, expected_no_clarification_total),
        'expected_clarification_rate_proxy': ratio(expected_clarification_detected_total, expected_clarification_total),
        'ambiguity_resolution_success_proxy': ratio(
            ambiguity_resolution_adjudicated_success_total,
            ambiguity_resolution_adjudicated_total,
        ),
        'wrong_confident_answer_rate': wrong_confident_answer_rate,
        'unnecessary_clarification_rate': unnecessary_clarification_rate,
        'ambiguity_resolution_success': ambiguity_resolution_success,
    }
    claim_grade_blockers: list[str] = []
    if floor_policy is None:
        claim_grade_blockers.append('floor policy missing')
    if overall_weight_total <= 0:
        claim_grade_blockers.append('no weighted certification inputs available')
    if overall_adjudication_weight_coverage != 1.0:
        rendered = 'None' if overall_adjudication_weight_coverage is None else f'{overall_adjudication_weight_coverage:.3f}'
        claim_grade_blockers.append(f'adjudication coverage incomplete ({rendered})')
    if reviewed_failure_class_missing:
        claim_grade_blockers.append(f'{reviewed_failure_class_missing} reviewed failures still lack canonical failure_class labels')
    if adjudicated_replay_conflicts:
        claim_grade_blockers.append(
            f'adjudicated replay conflicts present: {", ".join(adjudicated_replay_conflicts)}'
        )
    if failing_strata:
        claim_grade_blockers.append(f'required strata failed: {", ".join(failing_strata)}')
    if missing_required_strata:
        claim_grade_blockers.append(f'required strata missing: {", ".join(missing_required_strata)}')
    if wrong_confident_answer_rate is None or unnecessary_clarification_rate is None or ambiguity_resolution_success is None:
        claim_grade_blockers.append('semantic metrics are still proxy-backed, not final claim-grade semantic measures')
    claim_grade_ready = len(claim_grade_blockers) == 0

    report = {
        'run_id': f"score-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'scoring_mode': (
            'claim_grade'
            if claim_grade_ready
            else 'adjudicated_structural'
            if adjudication_path is not None and adjudication_path.exists()
            else 'provisional_structural'
        ),
        'claim_grade_ready': claim_grade_ready,
        'claim_grade_blockers': claim_grade_blockers,
        'snapshot_id': snapshot_ids.pop() if len(snapshot_ids) == 1 else None,
        'floor_policy_path': str(args.floor_policy.resolve()) if floor_policy is not None else None,
        'floor_policy_sha256': sha256_file(args.floor_policy.resolve()) if floor_policy is not None else None,
        'raw_results_path': str(args.raw_results.resolve()),
        'raw_results_sha256': sha256_file(args.raw_results.resolve()),
        'input_datasets': dataset_inputs,
        'adjudication_records_path': str(adjudication_path) if adjudication_path is not None and adjudication_path.exists() else None,
        'adjudication_records_sha256': sha256_file(adjudication_path) if adjudication_path is not None and adjudication_path.exists() else None,
        'snapshot': {
            'session_count': len(session_results),
            'dataset_types': dict(counts_by_type),
            'dataset_tiers': dict(counts_by_tier),
            'holdout_splits': dict(counts_by_split),
            'adjudicated_session_count': adjudicated_records_total,
        },
        'metrics': metrics,
        'strata': {
            'provider_floor_policy_ready': floor_policy is not None,
            'floor_metric_source': floor_metric_source,
            'adjudicated_replay_conflicts': adjudicated_replay_conflicts,
            'failing_strata': failing_strata,
            'missing_required_strata': missing_required_strata,
            'direct_provider_success': direct_provider_success,
            'direct_provider_adjudicated_success': direct_provider_adjudicated_success,
            'evaluated_provider_floors': evaluated_provider_floors,
            'multiround_family_success': multiround_family_success,
            'multiround_family_adjudicated_success': multiround_family_adjudicated_success,
            'evaluated_multiround_family_floors': evaluated_multiround_family_floors,
            'ambiguity_family_success': ambiguity_family_success,
            'ambiguity_family_adjudicated_success': ambiguity_family_adjudicated_success,
            'evaluated_ambiguity_family_floors': evaluated_ambiguity_family_floors,
        },
        'session_results': session_results,
        'limitations': [
            'This scorer only checks provisional structural success (status/error/non-empty result).',
            'Optional adjudication labels can override the automated pass/fail view, but the scorer still does not compute the full claim-grade semantic/error-family metrics.',
            'The wrong_confident_answer_rate_proxy, unnecessary_clarification_rate_proxy, expected_clarification_rate_proxy, and ambiguity_resolution_success_proxy metrics are early behavioral proxies, not final claim-grade semantic metrics.',
            'Claim lower95 uses a stratified weighted Wilson estimator over certification-design strata; legacy pooled Kish lower95 fields remain only for back-compatibility diagnostics.',
            'It does not yet perform semantic adjudication or claim-grade weighted inference.',
            'Provider/family floor evaluation covers direct provider, multiround family, and ambiguity family strata, but not every future semantic-family floor.',
            'A public 99% claim must not rely on this report alone.'
        ],
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'output': str(output),
        'session_count': len(session_results),
        'overall_unweighted': metrics['provisional_structural_session_success']['overall_unweighted'],
        'direct_weighted_provisional_success': metrics['direct_weighted_provisional_success'],
        'claim_grade_ready': claim_grade_ready,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
