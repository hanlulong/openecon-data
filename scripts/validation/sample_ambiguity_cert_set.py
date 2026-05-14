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
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    CERTIFICATION_TARGETS,
    read_json,
    write_jsonl,
)

DEFAULT_STRATA = ROOT / 'validation' / 'manifests' / 'strata_definition-v2.json'
DEFAULT_SNAPSHOT = ROOT / 'validation' / 'manifests' / 'catalog_snapshot-2026-04-14.json'
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'datasets' / 'dev' / 'ambiguity-cert-candidates.jsonl'
SAMPLER_VERSION = 'ambiguity_sampler_v1'

FAMILY_TEMPLATES = {
    'transform_ambiguity': [
        ('US GDP', 'direct_answer', ['direct_answer_correct']),
        ('Canada employment', 'direct_answer', ['direct_answer_correct']),
        ('Germany inflation', 'direct_answer', ['direct_answer_correct']),
    ],
    'provider_ambiguity': [
        ('Japan GDP growth rate', 'direct_answer', ['direct_answer_correct']),
        ('France unemployment rate', 'direct_answer', ['direct_answer_correct']),
        ('US inflation rate', 'direct_answer', ['direct_answer_correct']),
    ],
    'scope_ambiguity': [
        ('trade balance', 'clarify', ['clarification_with_correct_options', 'multiround_clarification_to_correct_answer']),
        ('employment by region', 'clarify', ['clarification_with_correct_options', 'multiround_clarification_to_correct_answer']),
        ('GDP in Europe', 'clarify', ['clarification_with_correct_options', 'multiround_clarification_to_correct_answer']),
    ],
    'decomposition_ambiguity': [
        ('Canada unemployment by age', 'direct_answer', ['direct_answer_correct']),
        ('Ontario employment by gender', 'direct_answer', ['direct_answer_correct']),
        ('France inflation by category', 'clarify', ['clarification_with_correct_options', 'multiround_clarification_to_correct_answer']),
    ],
    'terminology_ambiguity': [
        ('government debt', 'clarify', ['clarification_with_correct_options', 'multiround_clarification_to_correct_answer']),
        ('interest rate', 'clarify', ['clarification_with_correct_options', 'multiround_clarification_to_correct_answer']),
        ('oil price', 'direct_answer', ['direct_answer_correct']),
    ],
    'dominant_interpretation_cases': [
        ('Bitcoin price', 'direct_answer', ['direct_answer_correct']),
        ('USD to EUR exchange rate', 'direct_answer', ['direct_answer_correct']),
        ('US unemployment rate', 'direct_answer', ['direct_answer_correct']),
    ],
}


def make_record(
    family: str,
    idx: int,
    query: str,
    expected_behavior: str,
    outcomes: list[str],
    *,
    snapshot_id: str,
    seed: int,
    holdout_split: str,
    dataset_tier: str,
    family_total_count: int,
    family_sample_count: int,
    certification_target: str = CERTIFICATION_TARGET_USER_ANSWERABILITY,
) -> dict:
    sampling_probability = (family_sample_count / family_total_count) if family_total_count else None
    selection_weight = (1.0 / sampling_probability) if sampling_probability else None
    return {
        'id': f'amb-{family}-{idx:06d}',
        'dataset_tier': dataset_tier,
        'evaluation_target': certification_target,
        'provenance': {
            'snapshot_id': snapshot_id,
            'sampler_version': SAMPLER_VERSION,
            'sampling_probability': sampling_probability,
            'selection_weight': selection_weight,
            'holdout_split': holdout_split,
            'seed': seed,
            'certification_target': certification_target,
            'generation_mode': 'ambiguity_template_bootstrap',
            'family': family,
        },
        'query': query,
        'expected_behavior': expected_behavior,
        'gold': {
            'evaluation_target': certification_target,
            'acceptable_outcomes': outcomes,
            'required_option_tags': None,
            'unnecessary_clarification': expected_behavior == 'direct_answer',
            'human_review_required': True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate ambiguity-session certification candidates from the ambiguity allocation plan.')
    parser.add_argument('--strata', type=Path, default=DEFAULT_STRATA)
    parser.add_argument('--snapshot', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--scale', type=float, default=1.0, help='Scale family allocations for demo runs (e.g. 0.05)')
    parser.add_argument('--seed', type=int, default=20260414)
    parser.add_argument('--dataset-tier', default='dev')
    parser.add_argument('--holdout-split', default='candidate')
    parser.add_argument(
        '--certification-target',
        choices=CERTIFICATION_TARGETS,
        default=CERTIFICATION_TARGET_USER_ANSWERABILITY,
        help='Evaluate whether the real user gets the needed answer.',
    )
    args = parser.parse_args()

    strata = read_json(args.strata.resolve())
    snapshot = read_json(args.snapshot.resolve())
    family_alloc = dict(strata['ambiguity_session_plan']['family_allocation'])
    snapshot_id = f"{snapshot['snapshot_date']}:{str(snapshot['git_sha'])[:8]}:{snapshot['indicator_count']}"
    records = []
    counters = {family: 0 for family in family_alloc}
    for family, count in family_alloc.items():
        templates = FAMILY_TEMPLATES[family]
        scaled_count = max(1, round(count * max(args.scale, 0.0))) if args.scale > 0 else 0
        for i in range(scaled_count):
            counters[family] += 1
            query, behavior, outcomes = templates[i % len(templates)]
            records.append(
                make_record(
                    family,
                    counters[family],
                    query,
                    behavior,
                    outcomes,
                    snapshot_id=snapshot_id,
                    seed=args.seed,
                    holdout_split=args.holdout_split,
                    dataset_tier=args.dataset_tier,
                    family_total_count=count,
                    family_sample_count=scaled_count,
                    certification_target=args.certification_target,
                )
            )

    write_jsonl(args.output.resolve(), records)
    print(json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'output': str(args.output.resolve()),
        'records': len(records),
        'families': counters,
        'scale': args.scale,
        'snapshot_id': snapshot_id,
        'certification_target': args.certification_target,
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
