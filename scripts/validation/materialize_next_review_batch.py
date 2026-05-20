#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.common import (  # noqa: E402
    CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    CERTIFICATION_TARGETS,
    DEFAULT_DB,
    USER_ANSWERABILITY_INVENTORY_ONLY_RISK_REASONS,
    answerability_supportability_exclusion_reason,
    apply_selection_supportability_probe_query,
    certification_target_for_row,
    provider_family_key,
    sample_indicator_rows,
    selection_supportability_reason_for_row,
    write_jsonl,
)
from scripts.validation.common import audit_direct_query_shape, direct_query_specificity_score, family_success_adjustment  # noqa: E402
from scripts.validation.sample_direct_cert_set import build_record as build_direct_record  # noqa: E402
from scripts.validation.sample_direct_cert_set import (  # noqa: E402
    _merge_provider_anchor_rows,
    _user_answerability_sampling_anchor_reason,
)
from scripts.validation.sample_multiround_cert_set import (  # noqa: E402
    annotate as annotate_multiround,
    mixed_provider_switch,
    gdp_variant_cycle,
    comtrade_bilateral,
    statscan_decomposition,
    crypto_rotation,
    fx_rotation,
)
from scripts.validation.sample_ambiguity_cert_set import (  # noqa: E402
    FAMILY_TEMPLATES,
    make_record as make_ambiguity_record,
)

DEFAULT_SNAPSHOT = ROOT / 'validation' / 'manifests' / 'catalog_snapshot-2026-04-14.json'

MULTI_BUILDERS = {
    'provider_switch_chain': mixed_provider_switch,
    'transform_switch_chain': gdp_variant_cycle,
    'comtrade_bilateral_chain': comtrade_bilateral,
    'statscan_decomposition_chain': statscan_decomposition,
    'crypto_rotation_chain': crypto_rotation,
    'fx_pair_chain': fx_rotation,
}

SUPPORTABILITY_PREFILTER_MIN_ROWS = 10_000


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def snapshot_id(snapshot: dict) -> str:
    return f"{snapshot['snapshot_date']}:{str(snapshot['git_sha'])[:8]}:{snapshot['indicator_count']}"


def normalize_provider_name(value: str) -> str:
    return str(value or '').strip()


def normalize_batch_targets(targets: object) -> list[dict]:
    """Normalize review-expansion target shapes for materialization.

    ``build_review_expansion_plan.py`` writes targets as a mapping keyed by
    provider/family name with ``additional_target_sessions`` and
    ``recommended_total_n``.  The original materializer accepted only a list of
    records with ``name`` and ``planned_batch_sessions``.  Keep both contracts
    mechanical and schema-level: this adapter does not change sampling,
    provider selection, supportability filtering, or pass/fail semantics.
    """
    if not targets:
        return []

    if isinstance(targets, dict):
        normalized: list[dict] = []
        for name, payload in targets.items():
            if not isinstance(payload, dict):
                payload = {}
            record = dict(payload)
            record.setdefault('name', str(name))
            if 'planned_batch_sessions' not in record:
                record['planned_batch_sessions'] = int(record.get('additional_target_sessions') or 0)
            if 'target_n' not in record and 'recommended_total_n' in record:
                record['target_n'] = record.get('recommended_total_n')
            normalized.append(record)
        return normalized

    if isinstance(targets, list):
        return [dict(target) for target in targets if isinstance(target, dict)]

    raise TypeError(f'Unsupported batch target shape: {type(targets).__name__}')


def planned_batch_sessions(target: dict) -> int:
    return int(target.get('planned_batch_sessions') or target.get('additional_target_sessions') or 0)


def raw_selection_supportability_reason(row: dict) -> str | None:
    provider = str(row.get('provider') or '').strip()
    return selection_supportability_reason_for_row(
        {
            'provider_stratum': provider,
            'origin': {
                'source_provider': provider,
                'source_indicator_code': row.get('code'),
                'name': row.get('name'),
                'category': row.get('category'),
            },
        }
    )


def supportability_prefilter_samples(
    provider: str,
    samples: list[dict],
    count: int,
    *,
    certification_target: str,
    include_supportability_probes: bool,
    min_candidate_rows: int = SUPPORTABILITY_PREFILTER_MIN_ROWS,
) -> tuple[list[dict], int, bool]:
    """Drop provider-native unsupported rows before expensive record synthesis.

    This preserves the answerability/supportability boundary for large IMF
    expansion slices.  It is intentionally mechanical: it consults the same
    provider-native supportability reason used after record synthesis, never
    user-query semantics, and it only runs when probe rows are not allowed.
    """
    if (
        certification_target != CERTIFICATION_TARGET_USER_ANSWERABILITY
        or include_supportability_probes
    ):
        return samples, 0, False
    provider_upper = str(provider or '').strip().upper()
    if provider_upper not in {'IMF', 'BIS'}:
        return samples, 0, False
    if provider_upper == 'IMF' and len(samples) < min_candidate_rows:
        return samples, 0, False

    selectable: list[dict] = []
    excluded_count = 0
    for row in samples:
        if raw_selection_supportability_reason(row):
            excluded_count += 1
        else:
            selectable.append(row)

    if len(selectable) < count:
        raise RuntimeError(
            'user_answerability direct materialization could not fill '
            f'{provider} planned count {count} without selecting '
            f'{excluded_count} supportability-inventory candidates; '
            'split/supportability-inventory replacement is required'
        )

    return selectable, excluded_count, True


def prefixed_session_id(session_id: str, prefix: str = '') -> str:
    clean_prefix = str(prefix or '').strip().strip('-')
    if not clean_prefix:
        return session_id
    return f'{clean_prefix}-{session_id}'


def apply_certification_target(record: dict, certification_target: str) -> dict:
    record['evaluation_target'] = certification_target
    provenance = record.setdefault('provenance', {})
    if isinstance(provenance, dict):
        provenance['certification_target'] = certification_target
    gold = record.get('gold')
    if isinstance(gold, dict):
        gold['evaluation_target'] = certification_target
    for round_case in record.get('rounds') or []:
        if isinstance(round_case, dict):
            round_gold = round_case.setdefault('gold', {})
            if isinstance(round_gold, dict):
                round_gold['evaluation_target'] = certification_target
    return record


def direct_oversample_count(provider: str, count: int, provider_population: int) -> int:
    """Return the deterministic catalog sample size for direct-batch candidates.

    IMF has a very dense catalog surface where unsupported public surfaces can
    dominate a small random slice.  Oversample it more deeply so the review
    batch can still find provider-level executable rows without query-specific
    allowlisting.
    """
    provider_upper = str(provider or '').upper()
    if count >= 100:
        base_count = max(count * 2, count + 250)
    elif count >= 25:
        base_count = max(count * 20, count + 500)
    else:
        base_count = max(count * 50, count + 200)
    if provider_upper == 'IMF':
        if count >= 100:
            # Supportability-safe IMF answerability rows are sparse in the
            # frozen catalog.  Large lower-bound expansion slices must sample
            # deeply enough to find provider-native supported rows without
            # admitting supportability-probe rows.
            base_count = max(base_count, count * 1_000, 15_000)
        elif count >= 20:
            # Medium user-answerability IMF slices can be dominated by
            # provider-native public-surface supportability inventory.  Sample
            # deeply enough that same-provider replacements can fill evidence
            # batches without admitting supportability-probe rows.
            base_count = max(base_count, count * 500, 15_000)
        elif count >= 5:
            base_count = max(base_count, count * 500, 5_000)
    elif provider_upper == 'WORLDBANK' and count >= 100:
        base_count = max(base_count, count * 10, count + 1_000)
    return min(provider_population, base_count)


def select_quality_screened_direct_records(
    records: list[dict],
    count: int,
    *,
    include_supportability_probes: bool = False,
) -> list[dict]:
    def sort_key(record: dict) -> tuple:
        provenance = dict(record.get('provenance') or {})
        origin = dict(record.get('origin') or {})
        risk_level = str(provenance.get('query_quality_risk') or 'low')
        risk_rank = {'low': 0, 'medium': 1, 'high': 2}.get(risk_level, 3)
        reasons = list(provenance.get('query_quality_reasons') or [])
        selection_reasons = list(provenance.get('selection_quality_reasons') or reasons)
        inventory_only_reason_count = sum(
            1
            for reason in selection_reasons
            if reason in USER_ANSWERABILITY_INVENTORY_ONLY_RISK_REASONS
        )
        selection_supportability_reason = str(
            provenance.get('selection_supportability_reason') or ''
        ).strip()
        provider_anchor = bool(provenance.get('user_answerability_sampling_anchor'))
        specificity = direct_query_specificity_score(record)
        popularity = float(origin.get('popularity') or 0.0)
        if certification_target_for_row(record) == CERTIFICATION_TARGET_USER_ANSWERABILITY:
            return (
                1 if selection_supportability_reason else 0,
                1 if risk_rank >= 2 else 0,
                0 if provider_anchor else 1,
                inventory_only_reason_count,
                0 if popularity > 0 else 1,
                -int(popularity),
                -specificity,
                len(str(record.get('query') or '')),
                str(record.get('id') or ''),
            )
        risk_penalty = {'low': 0, 'medium': 10, 'high': 25}.get(risk_level, 30)
        effective_specificity = specificity - risk_penalty
        risk_group = 1 if risk_rank >= 2 else 0
        return (
            risk_group,
            -effective_specificity,
            risk_rank,
            len(reasons),
            len(str(record.get('query') or '')),
            str(record.get('id') or ''),
            )

    selectable_records = [
        record
        for record in records
        if include_supportability_probes
        or not answerability_supportability_exclusion_reason(record)
    ]
    ranked = sorted(selectable_records, key=sort_key)

    base_provider_family_caps = {
        "WORLDBANK": 1,
        "IMF": 2,
    }
    provider_category_caps = {
        ("WORLDBANK", "Education Statistics"): 6,
    }
    family_counts: dict[tuple[str, str], int] = {}
    category_counts: dict[tuple[str, str], int] = {}
    selected: list[dict] = []
    deferred: list[dict] = []

    for record in ranked:
        provider = str(record.get("provider_stratum") or record.get("provider") or "").upper()
        origin = dict(record.get("origin") or {})
        family_name = str(origin.get("name") or record.get("name") or record.get("query") or "")
        family = provider_family_key(provider, family_name)
        category = str(origin.get("category") or record.get("category") or "").strip()
        cap = base_provider_family_caps.get(provider)
        family_signal = family_success_adjustment(provider, family_name)
        if cap is not None and family_signal >= 4:
            cap += 2
        elif cap is not None and family_signal >= 2:
            cap += 1
        if cap and family:
            key = (provider, family)
            if family_counts.get(key, 0) >= cap:
                deferred.append(record)
                continue
            family_counts[key] = family_counts.get(key, 0) + 1
        category_cap = provider_category_caps.get((provider, category))
        if category_cap:
            category_key = (provider, category)
            if category_counts.get(category_key, 0) >= category_cap:
                deferred.append(record)
                continue
            category_counts[category_key] = category_counts.get(category_key, 0) + 1
        selected.append(record)
        if len(selected) >= count:
            return selected

    if len(selected) < count:
        for record in deferred:
            selected.append(record)
            if len(selected) >= count:
                break

    return selected[:count]


def materialize_direct(
    targets: list[dict],
    *,
    snapshot_meta: dict,
    seed: int,
    holdout_split: str,
    dataset_tier: str,
    db_path: Path,
    certification_target: str = CERTIFICATION_TARGET_USER_ANSWERABILITY,
    include_supportability_probes: bool = False,
    supportability_inventory: list[dict] | None = None,
    session_id_prefix: str = '',
) -> list[dict]:
    rows = []
    seq = 1
    provider_counts = dict(snapshot_meta.get('provider_counts') or {})
    for target in targets:
        provider = normalize_provider_name(target['name'])
        count = planned_batch_sessions(target)
        if count <= 0:
            continue
        provider_population = int(provider_counts.get(provider, count))
        oversample_count = direct_oversample_count(provider, count, provider_population)
        samples = sample_indicator_rows(provider, oversample_count, db_path=db_path.resolve(), seed=seed)
        if certification_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
            samples = _merge_provider_anchor_rows(provider, samples, db_path=db_path.resolve())
        samples, _prefiltered_supportability_count, _supportability_prefiltered = supportability_prefilter_samples(
            provider,
            samples,
            count,
            certification_target=certification_target,
            include_supportability_probes=include_supportability_probes,
        )
        candidate_records = []
        for row in samples:
            record = build_direct_record(
                row,
                seq,
                provider_count=provider_population,
                provider_sample_count=count,
                snapshot_id=snapshot_id(snapshot_meta),
                seed=seed,
                holdout_split=holdout_split,
                dataset_tier=dataset_tier,
                certification_target=certification_target,
            )
            record['id'] = prefixed_session_id(f"batch-direct-{provider.lower()}-{seq:06d}", session_id_prefix)
            quality = audit_direct_query_shape(record)
            record['provenance']['batch_plan'] = 'next_review_batch'
            supportability_reason = selection_supportability_reason_for_row(record)
            if supportability_reason:
                record['provenance']['selection_supportability_reason'] = supportability_reason
                if include_supportability_probes:
                    apply_selection_supportability_probe_query(record)
            quality = audit_direct_query_shape(record)
            record['provenance']['query_quality_risk'] = quality['risk_level']
            record['provenance']['query_quality_reasons'] = quality['reasons']
            selection_quality_record = dict(record)
            selection_quality_record['evaluation_target'] = CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY
            selection_quality_record['provenance'] = dict(record.get('provenance') or {})
            selection_quality_record['provenance']['certification_target'] = CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY
            selection_quality_record['gold'] = dict(record.get('gold') or {})
            selection_quality_record['gold']['evaluation_target'] = CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY
            selection_quality = audit_direct_query_shape(selection_quality_record)
            record['provenance']['selection_quality_reasons'] = selection_quality['reasons']
            anchor_reason = _user_answerability_sampling_anchor_reason(row)
            if anchor_reason:
                record['provenance']['user_answerability_sampling_anchor'] = anchor_reason
            candidate_records.append(record)
            seq += 1
        baseline_selected_records = select_quality_screened_direct_records(
            candidate_records,
            count,
            include_supportability_probes=True,
        )
        selected_records = select_quality_screened_direct_records(
            candidate_records,
            count,
            include_supportability_probes=include_supportability_probes,
        )
        if (
            certification_target == CERTIFICATION_TARGET_USER_ANSWERABILITY
            and not include_supportability_probes
        ):
            baseline_ids = {str(record.get('id') or '') for record in baseline_selected_records}
            replacement_records = [
                record
                for record in selected_records
                if str(record.get('id') or '') not in baseline_ids
            ]
            excluded_records = [
                record
                for record in baseline_selected_records
                if answerability_supportability_exclusion_reason(record)
            ]
            if excluded_records and supportability_inventory is not None:
                for index, excluded in enumerate(excluded_records):
                    provenance = dict(excluded.get('provenance') or {})
                    origin = dict(excluded.get('origin') or {})
                    replacement = replacement_records[index] if index < len(replacement_records) else None
                    item = {
                        'session_id': str(excluded.get('id') or ''),
                        'provider': provider,
                        'supportability_reason': answerability_supportability_exclusion_reason(excluded),
                        'source_indicator_code': origin.get('source_indicator_code'),
                        'source_indicator_name': origin.get('name'),
                        'query': excluded.get('query'),
                        'original_user_answerability_query': provenance.get('original_user_answerability_query'),
                        'supportability_probe_query': provenance.get('supportability_probe_query'),
                        'snapshot_id': provenance.get('snapshot_id'),
                        'selection_weight': provenance.get('selection_weight'),
                        'disposition': (
                            'excluded_with_replacement'
                            if replacement is not None
                            else 'excluded_pending_replacement'
                        ),
                        'replacement_session_ids': (
                            [str(replacement.get('id') or '')]
                            if replacement is not None
                            else []
                        ),
                    }
                    supportability_inventory.append(item)
            if len(selected_records) < count:
                excluded_count = sum(
                    1
                    for record in candidate_records
                    if answerability_supportability_exclusion_reason(record)
                )
                raise RuntimeError(
                    'user_answerability direct materialization could not fill '
                    f'{provider} planned count {count} without selecting '
                    f'{excluded_count} supportability-inventory candidates; '
                    'split/supportability-inventory replacement is required'
                )
        rows.extend(selected_records)
    return rows


def materialize_multiround(
    targets: list[dict],
    *,
    snapshot_meta: dict,
    seed: int,
    holdout_split: str,
    dataset_tier: str,
    certification_target: str = CERTIFICATION_TARGET_USER_ANSWERABILITY,
    session_id_prefix: str = '',
) -> list[dict]:
    rows = []
    counters = {name: 0 for name in MULTI_BUILDERS}
    for target in targets:
        family = str(target['name'])
        count = planned_batch_sessions(target)
        builder = MULTI_BUILDERS[family]
        counters[family] = max(counters.get(family, 0), int(target.get('current_n') or 0))
        for _ in range(count):
            counters[family] += 1
            session = builder(counters[family])
            session['id'] = prefixed_session_id(f"batch-{family}-{counters[family]:06d}", session_id_prefix)
            rows.append(
                apply_certification_target(
                    annotate_multiround(
                        session,
                        snapshot_id=snapshot_id(snapshot_meta),
                        seed=seed,
                        holdout_split=holdout_split,
                        dataset_tier=dataset_tier,
                        family_total_count=max(int(target.get('target_n') or count), count),
                        family_sample_count=count,
                    ),
                    certification_target,
                )
            )
    return rows


def materialize_ambiguity(
    targets: list[dict],
    *,
    snapshot_meta: dict,
    seed: int,
    holdout_split: str,
    dataset_tier: str,
    certification_target: str = CERTIFICATION_TARGET_USER_ANSWERABILITY,
    session_id_prefix: str = '',
) -> list[dict]:
    rows = []
    counters = {name: 0 for name in FAMILY_TEMPLATES}
    for target in targets:
        family = str(target['name'])
        count = planned_batch_sessions(target)
        templates = FAMILY_TEMPLATES[family]
        total_target = max(int(target.get('target_n') or count), count)
        counters[family] = max(counters.get(family, 0), int(target.get('current_n') or 0))
        for idx in range(count):
            query, behavior, outcomes = templates[counters[family] % len(templates)]
            counters[family] += 1
            record = apply_certification_target(
                make_ambiguity_record(
                    family,
                    counters[family],
                    query,
                    behavior,
                    outcomes,
                    snapshot_id=snapshot_id(snapshot_meta),
                    seed=seed,
                    holdout_split=holdout_split,
                    dataset_tier=dataset_tier,
                    family_total_count=total_target,
                    family_sample_count=count,
                ),
                certification_target,
            )
            record['id'] = prefixed_session_id(str(record.get('id') or ''), session_id_prefix)
            rows.append(record)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description='Materialize dataset JSONLs for the next planned review batch.')
    parser.add_argument('--batch-plan', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('--db-path', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=20260415)
    parser.add_argument('--dataset-tier', default='dev')
    parser.add_argument('--holdout-split', default='batch_review')
    parser.add_argument(
        '--certification-target',
        choices=CERTIFICATION_TARGETS,
        default=CERTIFICATION_TARGET_USER_ANSWERABILITY,
        help='Direct rows default to real user answerability; legacy_catalog_replay is inventory-only replay.',
    )
    parser.add_argument(
        '--include-supportability-probes',
        action='store_true',
        help=(
            'Allow rows marked with selection_supportability_reason into '
            'user_answerability direct output. Intended only for explicit '
            'supportability-inventory/probe runs, not answerability evidence.'
        ),
    )
    parser.add_argument(
        '--session-id-prefix',
        default='',
        help='Optional deterministic prefix for all emitted session ids to avoid cross-batch id collisions.',
    )
    args = parser.parse_args()

    batch_plan = load_json(args.batch_plan.resolve())
    snapshot_meta = load_json(args.snapshot.resolve())

    allocation = dict(batch_plan.get('allocation') or {})
    direct_targets = normalize_batch_targets((allocation.get('direct') or {}).get('targets') or [])
    multiround_targets = normalize_batch_targets((allocation.get('multiround') or {}).get('targets') or [])
    ambiguity_targets = normalize_batch_targets((allocation.get('ambiguity') or {}).get('targets') or [])

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    supportability_inventory: list[dict] = []
    direct_rows = materialize_direct(
        direct_targets,
        snapshot_meta=snapshot_meta,
        seed=args.seed,
        holdout_split=args.holdout_split,
        dataset_tier=args.dataset_tier,
        db_path=args.db_path,
        certification_target=args.certification_target,
        include_supportability_probes=args.include_supportability_probes,
        supportability_inventory=supportability_inventory,
        session_id_prefix=args.session_id_prefix,
    )
    multiround_rows = materialize_multiround(
        multiround_targets,
        snapshot_meta=snapshot_meta,
        seed=args.seed,
        holdout_split=args.holdout_split,
        dataset_tier=args.dataset_tier,
        certification_target=args.certification_target,
        session_id_prefix=args.session_id_prefix,
    )
    ambiguity_rows = materialize_ambiguity(
        ambiguity_targets,
        snapshot_meta=snapshot_meta,
        seed=args.seed,
        holdout_split=args.holdout_split,
        dataset_tier=args.dataset_tier,
        certification_target=args.certification_target,
        session_id_prefix=args.session_id_prefix,
    )

    direct_path = output_dir / 'next_batch_direct.jsonl'
    multiround_path = output_dir / 'next_batch_multiround.jsonl'
    ambiguity_path = output_dir / 'next_batch_ambiguity.jsonl'
    all_path = output_dir / 'next_batch_all.jsonl'
    write_jsonl(direct_path, direct_rows)
    write_jsonl(multiround_path, multiround_rows)
    write_jsonl(ambiguity_path, ambiguity_rows)
    write_jsonl(all_path, direct_rows + multiround_rows + ambiguity_rows)
    supportability_inventory_path = output_dir / 'supportability_inventory.json'
    supportability_inventory_payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'certification_target': args.certification_target,
        'include_supportability_probes': bool(args.include_supportability_probes),
        'items': supportability_inventory,
    }
    supportability_inventory_path.write_text(
        json.dumps(supportability_inventory_payload, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )

    print(
        json.dumps(
            {
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'output_dir': str(output_dir),
                'direct_records': len(direct_rows),
                'multiround_records': len(multiround_rows),
                'ambiguity_records': len(ambiguity_rows),
                'batch_size': len(direct_rows) + len(multiround_rows) + len(ambiguity_rows),
                'certification_target': args.certification_target,
                'supportability_inventory': str(supportability_inventory_path),
                'supportability_inventory_items': len(supportability_inventory),
            },
            indent=2,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
