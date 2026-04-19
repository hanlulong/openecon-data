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

from scripts.validation.common import (
    audit_direct_query_shape,
    DEFAULT_DB,
    default_query_for_row,
    direct_query_specificity_score,
    infer_scope_family,
    infer_transform_family,
    read_json,
    sample_indicator_rows,
    top_tokens,
    write_jsonl,
)

DEFAULT_STRATA = ROOT / 'validation' / 'manifests' / 'strata_definition-v2.json'
DEFAULT_SNAPSHOT = ROOT / 'validation' / 'manifests' / 'catalog_snapshot-2026-04-14.json'
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'datasets' / 'dev' / 'direct-cert-candidates.jsonl'
SAMPLER_VERSION = 'direct_sampler_v1'


def build_record(row: dict, seq: int, *, provider_count: int, provider_sample_count: int, snapshot_id: str, seed: int, holdout_split: str, dataset_tier: str) -> dict:
    provider = str(row['provider'])
    name = str(row.get('name') or '').strip()
    description = str(row.get('description') or '').strip()
    transform = infer_transform_family(name, description, str(row.get('unit') or ''), str(row.get('code') or ''))
    scope = infer_scope_family(provider, row.get('coverage'))
    tokens = top_tokens(name, description, str(row.get('category') or ''), str(row.get('subcategory') or ''))
    sampling_probability = (provider_sample_count / provider_count) if provider_count else None
    selection_weight = (1.0 / sampling_probability) if sampling_probability else None
    return {
        'id': f'direct-{provider.lower()}-{seq:06d}',
        'dataset_tier': dataset_tier,
        'provider_stratum': provider,
        'query_family': 'direct_single_series',
        'transform_family': transform,
        'scope_family': scope,
        'ambiguity_class': 'clearly_answerable',
        'query': default_query_for_row(row),
        'origin': {
            'indicator_id': row.get('id'),
            'source_indicator_code': row.get('code'),
            'source_provider': provider,
            'name': name,
            'description': description,
            'category': row.get('category'),
            'subcategory': row.get('subcategory'),
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
            'generation_mode': 'provider_weighted_with_floor',
        },
        'gold': {
            'required_concept_tags': tokens[:4],
            'required_transform_tags': [transform],
            'required_country_scope': None,
            'required_time_scope': None,
            'accepted_providers': [provider],
            'provider_equivalence_allowed': False,
            'clarification_expected': False,
            'clarification_allowed': False,
            'forbidden_tags': None,
            'forbidden_scopes': None,
            'human_review_required': True,
        },
    }


def _select_quality_screened_records(records: list[dict], count: int) -> list[dict]:
    def sort_key(record: dict) -> tuple[int, int, int, int, str]:
        provenance = dict(record.get('provenance') or {})
        risk_level = str(provenance.get('query_quality_risk') or 'low')
        risk_rank = {'low': 0, 'medium': 1, 'high': 2}.get(risk_level, 3)
        reasons = list(provenance.get('query_quality_reasons') or [])
        specificity = direct_query_specificity_score(record)
        risk_penalty = {'low': 0, 'medium': 10, 'high': 25}.get(risk_level, 30)
        effective_specificity = specificity - risk_penalty
        return (
            risk_rank,
            -effective_specificity,
            len(reasons),
            len(str(record.get('query') or '')),
            str(record.get('id') or ''),
        )

    ranked = sorted(records, key=sort_key)
    return ranked[:count]


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
        oversample_count = min(provider_population, max(scaled_count * 50, scaled_count + 200))
        samples = sample_indicator_rows(provider, oversample_count, db_path=args.db_path.resolve(), seed=args.seed)
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
            )
            quality = audit_direct_query_shape(record)
            record['provenance']['query_quality_risk'] = quality['risk_level']
            record['provenance']['query_quality_reasons'] = quality['reasons']
            candidate_records.append(record)
            seq += 1
        rows_out.extend(_select_quality_screened_records(candidate_records, scaled_count))

    write_jsonl(args.output.resolve(), rows_out)
    print(json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'output': str(args.output.resolve()),
        'records': len(rows_out),
        'scale': scale,
        'snapshot_id': snapshot_id,
        'dataset_tier': args.dataset_tier,
        'holdout_split': args.holdout_split,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
