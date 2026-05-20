#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.common import (
    answerability_supportability_exclusion_reason,
    apply_selection_supportability_probe_query,
    audit_direct_query_shape,
    CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    CERTIFICATION_TARGETS,
    certification_target_for_row,
    DEFAULT_DB,
    default_query_for_row,
    direct_query_specificity_score,
    infer_scope_family,
    infer_transform_family,
    imf_public_sdmx_runtime_family,
    read_json,
    sample_indicator_rows,
    selection_supportability_reason_for_row,
    top_tokens,
    USER_ANSWERABILITY_INVENTORY_ONLY_RISK_REASONS,
    write_jsonl,
)

DEFAULT_STRATA = ROOT / 'validation' / 'manifests' / 'strata_definition-v2.json'
DEFAULT_SNAPSHOT = ROOT / 'validation' / 'manifests' / 'catalog_snapshot-2026-04-14.json'
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'datasets' / 'dev' / 'direct-cert-candidates.jsonl'
SAMPLER_VERSION = 'direct_sampler_v2'
DEFAULT_OVERSAMPLE_FACTOR = 8
DEFAULT_OVERSAMPLE_CAP = 20000
DEFAULT_OVERSAMPLE_BUFFER = 500


def build_record(
    row: dict,
    seq: int,
    *,
    provider_count: int,
    provider_sample_count: int,
    snapshot_id: str,
    seed: int,
    holdout_split: str,
    dataset_tier: str,
    certification_target: str = CERTIFICATION_TARGET_USER_ANSWERABILITY,
) -> dict:
    provider = str(row['provider'])
    name = str(row.get('name') or '').strip()
    description = str(row.get('description') or '').strip()
    unit = str(row.get('unit') or '').strip()
    transform = infer_transform_family(name, description, unit, str(row.get('code') or ''))
    scope = infer_scope_family(provider, row.get('coverage'))
    tokens = top_tokens(
        name,
        description,
        str(row.get('category') or ''),
        str(row.get('subcategory') or ''),
    )
    unit_tokens = [token for token in top_tokens(unit, limit=2) if token != 'economic'] if unit else []
    required_concept_tags = tokens[:4] + [
        token for token in unit_tokens if token not in tokens[:4]
    ][:2]
    sampling_probability = (provider_sample_count / provider_count) if provider_count else None
    selection_weight = (1.0 / sampling_probability) if sampling_probability else None
    return {
        'id': f'direct-{provider.lower()}-{seq:06d}',
        'dataset_tier': dataset_tier,
        'evaluation_target': certification_target,
        'provider_stratum': provider,
        'query_family': 'direct_single_series',
        'transform_family': transform,
        'scope_family': scope,
        'ambiguity_class': 'clearly_answerable',
        'query': default_query_for_row(row, certification_target=certification_target),
        'origin': {
            'indicator_id': row.get('id'),
            'source_indicator_code': row.get('code'),
            'source_provider': provider,
            'name': name,
            'description': description,
            'category': row.get('category'),
            'subcategory': row.get('subcategory'),
            'unit': unit or None,
            'coverage': row.get('coverage'),
            'frequency': row.get('frequency'),
            'keywords': row.get('keywords'),
            'synonyms': row.get('synonyms'),
            'raw_metadata': row.get('raw_metadata'),
            'popularity': row.get('popularity'),
            'last_updated': row.get('last_updated'),
        },
        'provenance': {
            'snapshot_id': snapshot_id,
            'sampler_version': SAMPLER_VERSION,
            'sampling_probability': sampling_probability,
            'selection_weight': selection_weight,
            'holdout_split': holdout_split,
            'seed': seed,
            'certification_target': certification_target,
            'generation_mode': 'provider_weighted_with_floor',
            'legacy_catalog_replay_required': certification_target == CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
        },
        'gold': {
            'evaluation_target': certification_target,
            'required_concept_tags': required_concept_tags,
            'required_transform_tags': [transform],
            'required_country_scope': None,
            'required_time_scope': None,
            'accepted_providers': [provider],
            'provider_equivalence_allowed': False,
            'clarification_expected': False,
            'clarification_allowed': False,
            'forbidden_tags': None,
            'forbidden_scopes': None,
            'legacy_source_indicator_code_required': certification_target == CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
            'legacy_catalog_row_replay_required': certification_target == CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
            'answer_acceptance_criteria': [
                'answer satisfies the user intent expressed in the query',
                'answer uses an accepted provider or an explicitly equivalent source',
                'answer respects requested country, time, transform, and decomposition scope',
                'legacy source_indicator_code is not required unless the query itself asks for that code',
            ],
            'human_review_required': True,
        },
    }


def _select_quality_screened_records(
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
                risk_rank,
                0 if provider_anchor else 1,
                inventory_only_reason_count,
                0 if popularity > 0 else 1,
                -int(popularity),
                -specificity,
                len(str(record.get('query') or '')),
                len(reasons),
                str(record.get('id') or ''),
            )
        risk_penalty = {'low': 0, 'medium': 10, 'high': 25}.get(risk_level, 30)
        effective_specificity = specificity - risk_penalty
        return (
            risk_rank,
            -effective_specificity,
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
    return ranked[:count]


def _user_answerability_sampling_anchor_reason(row: dict) -> str | None:
    """Return provider-native candidate-selection evidence for real-user rows.

    This is deliberately only a sampler prior.  It does not make the runtime
    result correct, it does not map old catalog labels to new codes, and it
    does not create supportability/pass judgments.  It just prevents
    row-count-heavy stale inventory surfaces from crowding out provider-native
    current answer surfaces in the first user-answerability holdout chunks.
    """
    provider = str(row.get('provider') or '').upper()
    category = str(row.get('category') or '').strip().lower()
    code = str(row.get('code') or '').strip()
    name = str(row.get('name') or '').strip()
    if provider == 'IMF':
        if category == 'weo':
            return 'imf_provider_native_weo_surface'
        sdmx_family = imf_public_sdmx_runtime_family(code, name, str(row.get('category') or ''))
        if sdmx_family:
            return f'imf_provider_native_sdmx_{sdmx_family}'
    return None


def _provider_anchor_rows(provider: str, *, db_path: Path) -> list[dict]:
    provider_upper = provider.upper()
    if provider_upper != 'IMF':
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        '''
        SELECT id, provider, code, name, description, category, subcategory, unit,
               frequency, coverage, start_date, end_date, keywords, synonyms,
               raw_metadata, popularity, last_updated
        FROM indicators
        WHERE provider = ?
          AND lower(coalesce(category, '')) = 'weo'
        ORDER BY id
        ''',
        (provider,),
    ).fetchall()
    con.close()
    return [dict(row) for row in rows]


def _merge_provider_anchor_rows(provider: str, sampled_rows: list[dict], *, db_path: Path) -> list[dict]:
    anchors = _provider_anchor_rows(provider, db_path=db_path)
    if not anchors:
        return sampled_rows
    by_id: dict[object, dict] = {row.get('id'): row for row in sampled_rows}
    for row in anchors:
        by_id.setdefault(row.get('id'), row)
    return list(by_id.values())


def provider_oversample_target(
    provider_population: int,
    provider_sample_count: int,
    *,
    oversample_factor: int = DEFAULT_OVERSAMPLE_FACTOR,
    oversample_cap: int = DEFAULT_OVERSAMPLE_CAP,
    oversample_buffer: int = DEFAULT_OVERSAMPLE_BUFFER,
) -> int:
    if provider_population <= 0 or provider_sample_count <= 0:
        return 0

    factor_target = provider_sample_count * max(1, int(oversample_factor))
    capped_factor_target = min(factor_target, max(provider_sample_count, int(oversample_cap)))
    buffered_target = provider_sample_count + max(0, int(oversample_buffer))
    return min(provider_population, max(buffered_target, capped_factor_target))


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate direct-session certification candidates from the frozen provider allocation plan.')
    parser.add_argument('--db-path', type=Path, default=DEFAULT_DB)
    parser.add_argument('--strata', type=Path, default=DEFAULT_STRATA)
    parser.add_argument('--snapshot', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--seed', type=int, default=20260414)
    parser.add_argument('--scale', type=float, default=1.0, help='Scale allocations for demo runs (e.g. 0.02)')
    parser.add_argument('--dataset-tier', default='dev')
    parser.add_argument('--holdout-split', default='candidate')
    parser.add_argument('--oversample-factor', type=int, default=DEFAULT_OVERSAMPLE_FACTOR)
    parser.add_argument('--oversample-cap', type=int, default=DEFAULT_OVERSAMPLE_CAP)
    parser.add_argument('--oversample-buffer', type=int, default=DEFAULT_OVERSAMPLE_BUFFER)
    parser.add_argument(
        '--certification-target',
        choices=CERTIFICATION_TARGETS,
        default=CERTIFICATION_TARGET_USER_ANSWERABILITY,
        help='Evaluate real user answerability by default; use legacy_catalog_replay only for inventory replay.',
    )
    parser.add_argument(
        '--include-supportability-probes',
        action='store_true',
        help=(
            'Allow rows marked with selection_supportability_reason into '
            'user_answerability output. Intended only for explicit '
            'supportability-inventory/probe runs, not answerability evidence.'
        ),
    )
    args = parser.parse_args()

    strata = read_json(args.strata.resolve())
    snapshot = read_json(args.snapshot.resolve())
    allocation: dict[str, int] = dict(strata['direct_session_plan']['provider_allocation'])
    snapshot_id = f"{snapshot['snapshot_date']}:{str(snapshot['git_sha'])[:8]}:{snapshot['indicator_count']}"
    provider_counts = dict(snapshot['provider_counts'])
    scale = max(args.scale, 0.0)
    rows_out = []
    seq = 1
    for provider, count in allocation.items():
        scaled_count = max(1, round(count * scale)) if scale > 0 else 0
        if scaled_count == 0:
            continue
        provider_population = int(provider_counts[provider])
        oversample_count = provider_oversample_target(
            provider_population,
            scaled_count,
            oversample_factor=args.oversample_factor,
            oversample_cap=args.oversample_cap,
            oversample_buffer=args.oversample_buffer,
        )
        samples = sample_indicator_rows(provider, oversample_count, db_path=args.db_path.resolve(), seed=args.seed)
        if args.certification_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
            samples = _merge_provider_anchor_rows(provider, samples, db_path=args.db_path.resolve())
        candidate_records = []
        for row in samples:
            record = build_record(
                row,
                seq,
                provider_count=provider_population,
                provider_sample_count=scaled_count,
                snapshot_id=snapshot_id,
                seed=args.seed,
                holdout_split=args.holdout_split,
                dataset_tier=args.dataset_tier,
                certification_target=args.certification_target,
            )
            supportability_reason = selection_supportability_reason_for_row(record)
            if supportability_reason:
                record['provenance']['selection_supportability_reason'] = supportability_reason
                if args.include_supportability_probes:
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
        selected_records = _select_quality_screened_records(
            candidate_records,
            scaled_count,
            include_supportability_probes=args.include_supportability_probes,
        )
        if (
            args.certification_target == CERTIFICATION_TARGET_USER_ANSWERABILITY
            and not args.include_supportability_probes
            and len(selected_records) < scaled_count
        ):
            excluded_count = sum(
                1
                for record in candidate_records
                if answerability_supportability_exclusion_reason(record)
            )
            raise RuntimeError(
                'user_answerability direct sampler could not fill '
                f'{provider} planned count {scaled_count} without selecting '
                f'{excluded_count} supportability-inventory candidates; '
                'split/supportability-inventory replacement is required'
            )
        rows_out.extend(selected_records)

    write_jsonl(args.output.resolve(), rows_out)
    print(json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'output': str(args.output.resolve()),
        'records': len(rows_out),
        'scale': scale,
        'snapshot_id': snapshot_id,
        'dataset_tier': args.dataset_tier,
        'holdout_split': args.holdout_split,
        'certification_target': args.certification_target,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
