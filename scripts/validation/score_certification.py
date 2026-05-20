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
CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY = 'legacy_catalog_replay'
CERTIFICATION_TARGET_USER_ANSWERABILITY = 'user_answerability'
DEFAULT_SNAPSHOT_MANIFEST = ROOT / 'validation' / 'manifests' / 'catalog_snapshot-2026-04-14.json'
DEFAULT_STRATA_MANIFEST = ROOT / 'validation' / 'manifests' / 'strata_definition-v1.json'


def normalize_certification_target(value: object, *, default: str = CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY) -> str:
    raw = str(value or '').strip().lower()
    if raw in {'user', 'real_user', 'user_answerability', 'real_user_answerability', 'answerability'}:
        return CERTIFICATION_TARGET_USER_ANSWERABILITY
    if raw in {'legacy', 'catalog', 'catalog_replay', 'legacy_catalog', 'legacy_catalog_replay'}:
        return CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY
    return default


def certification_target_for_session(session: dict[str, Any]) -> str:
    provenance = session.get('provenance') if isinstance(session.get('provenance'), dict) else {}
    gold = session.get('gold') if isinstance(session.get('gold'), dict) else {}
    return normalize_certification_target(
        session.get('evaluation_target')
        or session.get('certification_target')
        or provenance.get('certification_target')
        or provenance.get('evaluation_target')
        or gold.get('evaluation_target')
        or gold.get('certification_target')
    )


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


def expected_fail_closed_provider_country_pass(
    session: dict[str, Any],
    rows: list[dict[str, Any]],
) -> bool:
    """Return True when gold expects a clear provider/country no-data answer.

    Real-user answerability is not always "return a populated series".  If a
    user explicitly asks an impossible provider/country combination, the correct
    answer is a fail-closed explanation with no substituted data.  This function
    only activates from explicit gold metadata; it is not a semantic matching
    shortcut.
    """
    gold = session.get('gold') if isinstance(session.get('gold'), dict) else {}
    expected = str(gold.get('expected_outcome') or '').strip().lower()
    if expected != 'fail_closed_provider_country_unavailable':
        return False
    if certification_target_for_session(session) != CERTIFICATION_TARGET_USER_ANSWERABILITY:
        return False
    if not rows:
        return False
    if any(int(row.get('status_code') or 0) != 200 for row in rows):
        return False
    if any(int(row.get('series_count') or 0) > 0 for row in rows):
        return False
    if any(row.get('providers') or row.get('series_ids') for row in rows):
        return False

    forbidden_countries = {
        str(country).strip().lower()
        for country in (gold.get('must_not_return_countries') or [])
        if str(country or '').strip()
    }
    returned_countries = {
        str(country).strip().lower()
        for row in rows
        for country in (row.get('countries') or [])
        if str(country or '').strip()
    }
    if forbidden_countries and returned_countries.intersection(forbidden_countries):
        return False
    if returned_countries:
        return False

    evidence = ' '.join(
        str(row.get(key) or '')
        for row in rows
        for key in ('error', 'message')
    ).lower()
    return (
        'data_not_available' in evidence
        and (
            'provider/country not available' in evidence
            or 'only covers united states' in evidence
            or 'country scope' in evidence
        )
    )


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
        and scoring_weight(row) > 0
    ]
    if not eligible:
        return None

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        grouped[str(row.get('design_stratum') or '<missing>')].append(row)

    total_weight = sum(scoring_weight(row) for row in eligible)
    if total_weight <= 0:
        return None

    overall_success = 0.0
    lower95 = 0.0
    stratum_reports: dict[str, dict[str, Any]] = {}
    for stratum, rows in sorted(grouped.items()):
        weights = [scoring_weight(row) for row in rows]
        stratum_weight = sum(weights)
        if stratum_weight <= 0:
            continue
        pass_weight = sum(
            scoring_weight(row)
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


def resolve_manifest_path(raw_path: Any, *, default: Path) -> Path:
    if raw_path is None:
        return default
    path = Path(str(raw_path))
    return path if path.is_absolute() else ROOT / path


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


def normalize_multiround_family_name(value: str) -> str:
    raw = str(value or '').strip()
    aliases = {
        'provider_switch_chains': 'provider_switch_chain',
        'transform_switch_chains': 'transform_switch_chain',
        'comtrade_bilateral_chains': 'comtrade_bilateral_chain',
        'statscan_decomposition_chains': 'statscan_decomposition_chain',
    }
    return aliases.get(raw, raw)


def cumulative_design_population_totals(floor_policy: dict[str, Any] | None) -> dict[str, float]:
    """Return frozen design-population totals keyed by score design stratum.

    Incremental review batches carry batch-local inverse-probability weights.
    Those weights are useful provenance for a single batch, but summing them
    across several batches can make a repeatedly sampled stratum look like a
    larger share of the frozen claim population.  The cumulative scorer fixes
    that by rescaling row weights inside each design stratum so the stratum
    totals remain anchored to the frozen snapshot/strata manifests.
    """
    policy = floor_policy or {}
    snapshot_path = resolve_manifest_path(policy.get('snapshot_manifest_path'), default=DEFAULT_SNAPSHOT_MANIFEST)
    strata_path = resolve_manifest_path(policy.get('strata_manifest_path'), default=DEFAULT_STRATA_MANIFEST)

    totals: dict[str, float] = {}
    snapshot = load_json(snapshot_path)
    if snapshot:
        for provider, count in dict(snapshot.get('provider_counts') or {}).items():
            try:
                value = float(count)
            except (TypeError, ValueError):
                continue
            if value > 0:
                totals[f'direct_provider:{provider}'] = value

    strata = load_json(strata_path)
    if strata:
        for family, count in dict(((strata.get('multiround_session_plan') or {}).get('family_allocation')) or {}).items():
            try:
                value = float(count)
            except (TypeError, ValueError):
                continue
            if value > 0:
                if str(family).strip() == 'crypto_fx_rapid_switch_chains':
                    # v1 grouped these two runtime families together; v2 split
                    # them.  Preserve the historical total without making one
                    # family absorb the other's design population.
                    totals['multiround_family:crypto_rotation_chain'] = value / 2
                    totals['multiround_family:fx_pair_chain'] = value / 2
                else:
                    totals[f'multiround_family:{normalize_multiround_family_name(family)}'] = value
        for family, count in dict(((strata.get('ambiguity_session_plan') or {}).get('family_allocation')) or {}).items():
            try:
                value = float(count)
            except (TypeError, ValueError):
                continue
            if value > 0:
                totals[f'ambiguity_family:{family}'] = value
    return totals


def manifest_snapshot_id(snapshot: dict[str, Any] | None) -> str | None:
    if not snapshot:
        return None
    snapshot_date = str(snapshot.get('snapshot_date') or '').strip()
    git_sha = str(snapshot.get('git_sha') or '').strip()
    indicator_count = snapshot.get('indicator_count')
    if not snapshot_date or not git_sha or indicator_count is None:
        return None
    return f'{snapshot_date}:{git_sha[:8]}:{indicator_count}'


def raw_selection_weight(row: dict[str, Any]) -> float:
    return float(((row.get('provenance') or {}).get('selection_weight')) or 0.0)


def scoring_weight(row: dict[str, Any]) -> float:
    value = row.get('cumulative_design_weight')
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return raw_selection_weight(row)


def apply_cumulative_design_weights(
    records: list[dict[str, Any]],
    *,
    floor_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach claim-scoring weights that preserve frozen stratum shares.

    The adjustment is outcome-independent: it reads only each row's design
    stratum and provenance selection weight.  Within a stratum, raw weights are
    rescaled proportionally so their sum equals the fixed design-population
    total.  Strata without a manifest-backed total retain their raw weights and
    are disclosed in the report.
    """
    policy = floor_policy or {}
    snapshot = load_json(resolve_manifest_path(policy.get('snapshot_manifest_path'), default=DEFAULT_SNAPSHOT_MANIFEST))
    fixed_snapshot_id = manifest_snapshot_id(snapshot)
    record_snapshot_ids = sorted({
        str(((row.get('provenance') or {}).get('snapshot_id')) or '').strip()
        for row in records
        if str(((row.get('provenance') or {}).get('snapshot_id')) or '').strip()
    })
    snapshot_compatible = (
        not fixed_snapshot_id
        or not record_snapshot_ids
        or record_snapshot_ids == [fixed_snapshot_id]
    )
    totals = cumulative_design_population_totals(floor_policy) if snapshot_compatible else {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        if raw_selection_weight(row) > 0:
            grouped[str(row.get('design_stratum') or '<missing>')].append(row)

    stratum_reports: dict[str, dict[str, Any]] = {}
    fallback_strata: list[str] = []
    for stratum, rows in sorted(grouped.items()):
        raw_total = sum(raw_selection_weight(row) for row in rows)
        design_total = totals.get(stratum)
        if design_total is not None and design_total > 0 and raw_total > 0:
            scale = design_total / raw_total
            for row in rows:
                row['cumulative_design_weight'] = raw_selection_weight(row) * scale
            mode = 'fixed_design_population_rescale'
            effective_total = design_total
        else:
            for row in rows:
                row['cumulative_design_weight'] = raw_selection_weight(row)
            mode = 'raw_selection_weight_fallback'
            effective_total = raw_total
            fallback_strata.append(stratum)
        stratum_reports[stratum] = {
            'n': len(rows),
            'raw_weight_total': raw_total,
            'design_population_total': design_total,
            'scoring_weight_total': sum(scoring_weight(row) for row in rows),
            'mode': mode,
            'scale': (effective_total / raw_total) if raw_total > 0 else None,
        }

    return {
        'method': 'cumulative_fixed_design_stratum_rescale',
        'description': (
            'Batch-local selection weights are rescaled within each design stratum '
            'to fixed frozen snapshot/strata totals before weighted claim metrics are computed.'
        ),
        'outcome_independent': True,
        'fixed_design_snapshot_id': fixed_snapshot_id,
        'record_snapshot_ids': record_snapshot_ids,
        'snapshot_compatible': snapshot_compatible,
        'strata': stratum_reports,
        'fallback_strata': fallback_strata,
    }


def label_is_success(label: Any) -> bool | None:
    if label is None:
        return None
    normalized = str(label).strip().lower()
    if normalized in {'pass', 'passed', 'correct', 'accepted', 'success'}:
        return True
    if normalized:
        return False
    return None


DIRECT_ANSWER_ACCEPTANCE_OUTCOMES = {
    'direct_answer_correct',
    'dominant_direct_answer_correct',
}


def _normalize_outcome_label(value: Any) -> str:
    return str(value).strip().lower().replace('-', '_').replace(' ', '_')


def adjudication_accepts_direct_answer(adjudication_row: dict[str, Any] | None) -> bool:
    """Return True when human adjudication explicitly accepts a direct answer.

    Ambiguity templates are intentionally conservative and sometimes mark a
    prompt as "clarify" before real provider evidence is available. The claim
    target is real-user answerability, so a reviewed row may pass via a
    correctly sourced dominant direct answer. This function only reads the
    manual adjudication outcome; it does not infer correctness from query text
    or provider-specific keyword rules.
    """
    if not adjudication_row:
        return False

    values: list[Any] = []
    for key in ('accepted_outcome', 'final_outcome', 'adjudicated_outcome'):
        if key in adjudication_row:
            values.append(adjudication_row.get(key))
    for key in ('accepted_outcomes', 'acceptable_outcomes', 'adjudicated_outcomes'):
        raw = adjudication_row.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw is not None:
            values.append(raw)

    return any(
        _normalize_outcome_label(value) in DIRECT_ANSWER_ACCEPTANCE_OUTCOMES
        for value in values
    )


def canonical_failure_class(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized.endswith('_proxy'):
        return None
    return normalized


RESOLVED_SUPPORTABILITY_INVENTORY_DISPOSITIONS = {
    'backfilled',
    'excluded_replaced',
    'excluded_with_replacement',
    'replaced',
}


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or '').strip()]
    raw = str(value).strip()
    return [raw] if raw else []


def evaluate_supportability_inventory(
    inventory: dict[str, Any] | None,
    *,
    session_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Evaluate manifest-backed out-of-frame supportability diagnostics.

    Supportability inventory is intentionally not a pass override.  It is a
    disclosure/backfill ledger for rows excluded from an answerable-surface
    denominator after provider-native evidence proves the requested data were
    unavailable.  A resolved entry must point to a replacement/backfill session
    that is present in the scored bundle and structurally passes without its
    own supportability or runtime-unavailable blocker.
    """
    if inventory is None:
        return None

    items = inventory.get('items') or inventory.get('exclusions') or []
    if not isinstance(items, list):
        raise ValueError('supportability inventory must contain an items/exclusions list')

    by_session = {str(row.get('session_id') or ''): row for row in session_results}
    evaluated: list[dict[str, Any]] = []
    unresolved: list[str] = []
    provider_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    disposition_counts: Counter[str] = Counter()

    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError('supportability inventory entries must be objects')
        session_id = str(raw_item.get('session_id') or raw_item.get('original_session_id') or '').strip()
        provider = str(raw_item.get('provider') or raw_item.get('provider_stratum') or '<missing>').strip() or '<missing>'
        reason = str(raw_item.get('supportability_reason') or raw_item.get('reason') or '<missing>').strip() or '<missing>'
        disposition = str(raw_item.get('disposition') or '').strip().lower()
        replacement_ids = _as_string_list(raw_item.get('replacement_session_ids'))
        replacement_ids.extend(_as_string_list(raw_item.get('replacement_session_id')))
        # Keep insertion order while removing accidental duplicates.
        replacement_ids = list(dict.fromkeys(replacement_ids))

        provider_counts[provider] += 1
        reason_counts[reason] += 1
        disposition_counts[disposition or '<missing>'] += 1

        replacement_results = []
        for replacement_id in replacement_ids:
            result = by_session.get(replacement_id)
            replacement_results.append(
                {
                    'session_id': replacement_id,
                    'present': result is not None,
                    'provisional_structural_pass': bool(result and result.get('provisional_structural_pass')),
                    'supportability_blocked': bool(result and result.get('supportability_blocked')),
                    'runtime_unavailable': bool(result and result.get('runtime_unavailable')),
                }
            )

        resolved = (
            disposition in RESOLVED_SUPPORTABILITY_INVENTORY_DISPOSITIONS
            and bool(replacement_results)
            and all(
                item['present']
                and item['provisional_structural_pass']
                and not item['supportability_blocked']
                and not item['runtime_unavailable']
                for item in replacement_results
            )
        )
        if not resolved:
            unresolved.append(session_id or '<missing>')

        evaluated.append(
            {
                'raw_item': raw_item,
                'session_id': session_id,
                'provider': provider,
                'supportability_reason': reason,
                'disposition': disposition,
                'replacement_session_ids': replacement_ids,
                'replacement_results': replacement_results,
                'resolved': resolved,
            }
        )

    return {
        'total_items': len(evaluated),
        'resolved_items': sum(1 for item in evaluated if item['resolved']),
        'unresolved_items': len(unresolved),
        'unresolved_session_ids': unresolved,
        'provider_counts': dict(provider_counts),
        'reason_counts': dict(reason_counts),
        'disposition_counts': dict(disposition_counts),
        'items': evaluated,
    }


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
    adjudicated_direct_answer_accepted: bool = False,
) -> str | None:
    if adjudicated_pass is not True:
        return None
    if kind == 'multiround' and not all_pass:
        return 'adjudicated pass conflicts with multiround replay (one or more turns failed structural replay)'
    if expected_clarification is True and not session_clarification_detected:
        if adjudicated_direct_answer_accepted and session_answer_present:
            return None
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
    parser.add_argument(
        '--supportability-inventory',
        type=Path,
        default=None,
        help='Optional manifest of supportability/out-of-frame rows and required same-stratum replacements.',
    )
    parser.add_argument('--max-sessions', type=int, default=None)
    parser.add_argument('--start-index', type=int, default=0, help='0-based session index to start from before applying --max-sessions.')
    args = parser.parse_args()

    sessions: dict[str, dict[str, Any]] = {}
    session_order: list[str] = []
    floor_policy = load_json(args.floor_policy.resolve())
    supportability_inventory_path = args.supportability_inventory.resolve() if args.supportability_inventory is not None else None
    supportability_inventory_data = None
    if supportability_inventory_path is not None:
        supportability_inventory_data = load_json(supportability_inventory_path)
        if supportability_inventory_data is None:
            raise FileNotFoundError(f'supportability inventory not found: {supportability_inventory_path}')
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
    if args.start_index < 0:
        raise ValueError('--start-index must be 0 or greater')
    if args.max_sessions is not None and args.max_sessions < 0:
        raise ValueError('--max-sessions must be 0 or greater')
    selected_session_order = session_order[args.start_index:]
    if args.max_sessions is not None:
        selected_session_order = selected_session_order[:args.max_sessions]
    allowed_ids = set(selected_session_order)
    sessions = {sid: sessions[sid] for sid in selected_session_order if sid in allowed_ids}

    raw_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in iter_jsonl(args.raw_results.resolve()):
        raw_by_session[str(row.get('session_id') or '')].append(row)

    session_results = []
    counts_by_type = Counter()
    counts_by_tier = Counter()
    counts_by_split = Counter()
    counts_by_evaluation_target = Counter()
    provisional_pass_by_type = Counter()
    provisional_pass_by_split = Counter()
    provisional_pass_by_evaluation_target = Counter()
    adjudicated_pass_by_type = Counter()
    adjudicated_pass_by_split = Counter()
    adjudicated_pass_by_evaluation_target = Counter()
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
    supportability_blocked_total = 0
    supportability_blocked_by_provider = Counter()
    supportability_blocked_reason_counts = Counter()
    runtime_unavailable_total = 0
    runtime_unavailable_by_type = Counter()
    runtime_unavailable_reason_counts = Counter()

    for sid, session in sessions.items():
        kind = dataset_type(session)
        tier = str(session.get('dataset_tier') or '<missing>')
        split = str((session.get('provenance') or {}).get('holdout_split') or '<missing>')
        evaluation_target = certification_target_for_session(session)
        provider = str(session.get('provider_stratum') or (session.get('origin') or {}).get('source_provider') or '<missing>')
        family = family_for_session(session, kind)
        rows = sorted(raw_by_session.get(sid, []), key=lambda r: int(r.get('round_index') or 0))
        session_clarification_detected = any(bool(row.get('clarification_detected')) for row in rows)
        supportability_blocked = any(bool(row.get('supportability_blocked')) for row in rows)
        supportability_reasons = sorted({
            str(row.get('supportability_reason'))
            for row in rows
            if row.get('supportability_reason')
        })
        runtime_unavailable = any(bool(row.get('runtime_unavailable')) for row in rows)
        runtime_unavailable_reasons = sorted({
            str(row.get('runtime_unavailable_reason'))
            for row in rows
            if row.get('runtime_unavailable_reason')
        })
        session_answer_present = any(
            (int(row.get('series_count') or 0) > 0 or bool(row.get('response_text_present')))
            and not bool(row.get('clarification_detected'))
            for row in rows
        )
        expected_clarification = expected_clarification_for_session(session, kind)
        design_stratum = design_stratum_for_session(session, kind)
        all_pass = bool(rows) and all(structural_pass(r) for r in rows)
        expected_fail_closed_pass = (
            kind == 'direct'
            and expected_fail_closed_provider_country_pass(session, rows)
        )
        provisional_pass = (
            clarification_path_pass(rows) and session_clarification_detected
            if expected_clarification is True
            else (all_pass or expected_fail_closed_pass)
        )
        any_error = next((r.get('error') for r in rows if r.get('error')), None)
        snapshot_id = str((session.get('provenance') or {}).get('snapshot_id') or '').strip()
        if snapshot_id:
            snapshot_ids.add(snapshot_id)
        adjudication_row = adjudication_records.get(sid)
        final_label = adjudication_row.get('final_label') if adjudication_row else None
        adjudicated_pass = label_is_success(final_label)
        adjudicated_direct_answer_accepted = adjudication_accepts_direct_answer(adjudication_row)
        final_failure_class = canonical_failure_class(adjudication_row.get('failure_class')) if adjudication_row else None
        supportability_overrode_adjudication = False
        if supportability_blocked and adjudicated_pass is True:
            # A stale/manual adjudicated pass cannot certify a row that the
            # current public-provider supportability guard refuses to execute.
            # Keep the row reviewed for coverage, but score it as an unresolved
            # supportability failure rather than adding a misleading replay
            # conflict for a surface we intentionally did not call.
            adjudicated_pass = False
            final_failure_class = final_failure_class or 'supportability_blocked'
            supportability_overrode_adjudication = True
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
            session_answer_present=session_answer_present or expected_fail_closed_pass,
            adjudicated_direct_answer_accepted=adjudicated_direct_answer_accepted,
        )
        if replay_conflict is not None:
            adjudicated_replay_conflicts.append(f'{sid}: {replay_conflict}')
        result_record = {
            'session_id': sid,
            'dataset_type': kind,
            'dataset_tier': tier,
            'holdout_split': split,
            'evaluation_target': evaluation_target,
            'provider_stratum': provider,
            'family_stratum': family,
            'design_stratum': design_stratum,
            'provisional_structural_pass': provisional_pass,
            'expected_fail_closed_pass': expected_fail_closed_pass,
            'final_label': final_label,
            'final_failure_class': final_failure_class,
            'adjudicated_pass': adjudicated_pass,
            'adjudicated_direct_answer_accepted': adjudicated_direct_answer_accepted,
            'expected_clarification': expected_clarification,
            'clarification_detected': session_clarification_detected,
            'answer_present_without_clarification': session_answer_present,
            'supportability_blocked': supportability_blocked,
            'supportability_reasons': supportability_reasons,
            'supportability_overrode_adjudication': supportability_overrode_adjudication,
            'runtime_unavailable': runtime_unavailable,
            'runtime_unavailable_reasons': runtime_unavailable_reasons,
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
        counts_by_evaluation_target[evaluation_target] += 1
        if provisional_pass:
            provisional_pass_by_type[kind] += 1
            provisional_pass_by_split[split] += 1
            provisional_pass_by_evaluation_target[evaluation_target] += 1
        if adjudicated_pass:
            adjudicated_pass_by_type[kind] += 1
            adjudicated_pass_by_split[split] += 1
            adjudicated_pass_by_evaluation_target[evaluation_target] += 1
        if supportability_blocked:
            supportability_blocked_total += 1
            supportability_blocked_by_provider[provider] += 1
            for reason in supportability_reasons:
                supportability_blocked_reason_counts[reason] += 1
        if runtime_unavailable:
            runtime_unavailable_total += 1
            runtime_unavailable_by_type[kind] += 1
            for reason in runtime_unavailable_reasons:
                runtime_unavailable_reason_counts[reason] += 1

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
            if provisional_pass:
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

    design_weight_report = apply_cumulative_design_weights(
        session_results,
        floor_policy=floor_policy,
    )

    # Recompute all weighted claim metrics from cumulative design weights.
    # The first pass above builds unweighted outcomes and supportability
    # counters.  Weighted evidence is deliberately computed only after every
    # row can be rescaled against the cumulative frozen design frame; otherwise
    # incremental batches would inflate repeatedly sampled strata.
    weighted_totals_by_type = defaultdict(float)
    weighted_pass_totals_by_type = defaultdict(float)
    adjudicated_weighted_pass_totals_by_type = defaultdict(float)
    weighted_session_counts_by_type = Counter()
    direct_weights = []
    direct_pass_weights = []
    all_weights = []
    all_pass_weights = []
    all_adjudicated_pass_weights = []
    all_reviewed_weights = []
    expected_no_clarification_weight_total = 0.0
    expected_no_clarification_unnecessary_weight = 0.0
    expected_clarification_weight_total = 0.0
    expected_clarification_success_weight = 0.0
    wrong_confident_weight_total = 0.0

    for result_record in session_results:
        weight = scoring_weight(result_record)
        if weight <= 0:
            continue
        kind = str(result_record.get('dataset_type') or '<missing>')
        weighted_totals_by_type[kind] += weight
        weighted_session_counts_by_type[kind] += 1
        all_weights.append(weight)
        if result_record.get('provisional_structural_pass'):
            weighted_pass_totals_by_type[kind] += weight
            all_pass_weights.append(weight)
        if result_record.get('adjudicated_pass') is not None:
            all_reviewed_weights.append(weight)
        if result_record.get('adjudicated_pass'):
            adjudicated_weighted_pass_totals_by_type[kind] += weight
            all_adjudicated_pass_weights.append(weight)

        expected_clarification = result_record.get('expected_clarification')
        final_failure_class = result_record.get('final_failure_class')
        if expected_clarification is False:
            expected_no_clarification_weight_total += weight
            if final_failure_class == 'unnecessary_clarification':
                expected_no_clarification_unnecessary_weight += weight
        elif expected_clarification is True:
            expected_clarification_weight_total += weight
            if result_record.get('adjudicated_pass'):
                expected_clarification_success_weight += weight
        if result_record.get('adjudicated_pass') is False and final_failure_class == 'wrong_confident_answer':
            wrong_confident_weight_total += weight

        if kind == 'direct':
            direct_weights.append(weight)
            if result_record.get('provisional_structural_pass'):
                direct_pass_weights.append(weight)

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
    supportability_inventory_report = evaluate_supportability_inventory(
        supportability_inventory_data,
        session_results=session_results,
    )
    supportability_inventory_summary = supportability_inventory_report or {}

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
            'by_evaluation_target': {
                target: ratio(provisional_pass_by_evaluation_target[target], counts_by_evaluation_target[target]) or 0.0
                for target in counts_by_evaluation_target
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
            'by_evaluation_target': {
                target: ratio(
                    adjudicated_pass_by_evaluation_target[target],
                    sum(1 for r in session_results if r['evaluation_target'] == target and r['adjudicated_pass'] is not None),
                )
                for target in counts_by_evaluation_target
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
        'cumulative_design_weighting': design_weight_report,
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
        'supportability_blocked_sessions': supportability_blocked_total,
        'supportability_blocked_by_provider': dict(supportability_blocked_by_provider),
        'supportability_blocked_reason_counts': dict(supportability_blocked_reason_counts),
        'supportability_inventory_items': supportability_inventory_summary.get('total_items', 0),
        'supportability_inventory_resolved_items': supportability_inventory_summary.get('resolved_items', 0),
        'supportability_inventory_unresolved_items': supportability_inventory_summary.get('unresolved_items', 0),
        'runtime_unavailable_sessions': runtime_unavailable_total,
        'runtime_unavailable_by_type': dict(runtime_unavailable_by_type),
        'runtime_unavailable_reason_counts': dict(runtime_unavailable_reason_counts),
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
    claim_thresholds = dict((floor_policy or {}).get('claim_thresholds') or {})
    claim_grade_blockers: list[str] = []
    snapshot_id_values = sorted(snapshot_ids)
    if floor_policy is None:
        claim_grade_blockers.append('floor policy missing')
    if len(snapshot_id_values) > 1:
        rendered = ', '.join(snapshot_id_values[:5])
        suffix = '...' if len(snapshot_id_values) > 5 else ''
        claim_grade_blockers.append(f'mixed snapshot_ids in scored bundle: {rendered}{suffix}')
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
    if supportability_blocked_total:
        rendered = ', '.join(
            f'{provider}={count}'
            for provider, count in sorted(supportability_blocked_by_provider.items())
        )
        claim_grade_blockers.append(
            f'supportability-blocked certification sessions remain unresolved ({supportability_blocked_total}: {rendered})'
        )
    if supportability_inventory_report and supportability_inventory_report.get('unresolved_items'):
        unresolved_ids = supportability_inventory_report.get('unresolved_session_ids') or []
        rendered = ', '.join(str(item) for item in unresolved_ids[:10])
        suffix = f': {rendered}' if rendered else ''
        claim_grade_blockers.append(
            f'supportability inventory exclusions lack passing replacement '
            f'({supportability_inventory_report["unresolved_items"]}{suffix})'
        )
    if runtime_unavailable_total:
        rendered = ', '.join(
            f'{kind}={count}'
            for kind, count in sorted(runtime_unavailable_by_type.items())
        )
        claim_grade_blockers.append(
            f'runtime-unavailable certification sessions remain unresolved ({runtime_unavailable_total}: {rendered})'
        )
    if failing_strata:
        claim_grade_blockers.append(f'required strata failed: {", ".join(failing_strata)}')
    if missing_required_strata:
        claim_grade_blockers.append(f'required strata missing: {", ".join(missing_required_strata)}')
    required_evaluation_target = normalize_certification_target(
        (floor_policy or {}).get('certification_target'),
        default='',
    )
    if required_evaluation_target:
        mismatched_targets = {
            target: count
            for target, count in sorted(counts_by_evaluation_target.items())
            if target != required_evaluation_target
        }
        if mismatched_targets:
            rendered = ', '.join(f'{target}={count}' for target, count in mismatched_targets.items())
            claim_grade_blockers.append(
                f'certification target mismatch: policy requires {required_evaluation_target}; observed {rendered}'
            )
    if claim_metric_source:
        required_observed = claim_thresholds.get('weighted_session_success_min')
        if required_observed is not None:
            if claim_observed_success is None:
                claim_grade_blockers.append('claim_observed_success missing')
            elif claim_observed_success < float(required_observed):
                claim_grade_blockers.append(
                    f'claim_observed_success {claim_observed_success:.6f} below required {float(required_observed):.6f}'
                )
        required_lower95 = claim_thresholds.get('lower95_min')
        if required_lower95 is not None:
            if claim_lower95 is None:
                claim_grade_blockers.append('claim_lower95 missing')
            elif claim_lower95 < float(required_lower95):
                claim_grade_blockers.append(
                    f'claim_lower95 {claim_lower95:.6f} below required {float(required_lower95):.6f}'
                )
        wrong_confident_max = claim_thresholds.get('wrong_confident_answer_rate_max')
        if wrong_confident_max is not None and wrong_confident_answer_rate is not None:
            if wrong_confident_answer_rate > float(wrong_confident_max):
                claim_grade_blockers.append(
                    f'wrong_confident_answer_rate {wrong_confident_answer_rate:.6f} above required {float(wrong_confident_max):.6f}'
                )
        unnecessary_clarification_max = claim_thresholds.get('unnecessary_clarification_rate_max')
        if unnecessary_clarification_max is not None and unnecessary_clarification_rate is not None:
            if unnecessary_clarification_rate > float(unnecessary_clarification_max):
                claim_grade_blockers.append(
                    f'unnecessary_clarification_rate {unnecessary_clarification_rate:.6f} above required {float(unnecessary_clarification_max):.6f}'
                )
        ambiguity_resolution_min = claim_thresholds.get('ambiguity_resolution_success_min')
        if ambiguity_resolution_min is not None and ambiguity_resolution_success is not None:
            if ambiguity_resolution_success < float(ambiguity_resolution_min):
                claim_grade_blockers.append(
                    f'ambiguity_resolution_success {ambiguity_resolution_success:.6f} below required {float(ambiguity_resolution_min):.6f}'
                )
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
        'certification_target': required_evaluation_target or (next(iter(counts_by_evaluation_target)) if len(counts_by_evaluation_target) == 1 else None),
        'claim_grade_ready': claim_grade_ready,
        'claim_grade_blockers': claim_grade_blockers,
        'snapshot_id': snapshot_id_values[0] if len(snapshot_id_values) == 1 else None,
        'floor_policy_path': str(args.floor_policy.resolve()) if floor_policy is not None else None,
        'floor_policy_sha256': sha256_file(args.floor_policy.resolve()) if floor_policy is not None else None,
        'supportability_inventory_path': str(supportability_inventory_path) if supportability_inventory_path is not None else None,
        'supportability_inventory_sha256': sha256_file(supportability_inventory_path) if supportability_inventory_path is not None else None,
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
            'evaluation_targets': dict(counts_by_evaluation_target),
            'adjudicated_session_count': adjudicated_records_total,
        },
        'metrics': metrics,
        'supportability_inventory': supportability_inventory_report,
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
