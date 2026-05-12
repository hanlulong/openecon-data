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
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'datasets' / 'dev' / 'multiround-cert-candidates.jsonl'
SAMPLER_VERSION = 'multiround_sampler_v1'

GDP_TRIOS = [
    ('United States', 'China', 'Germany'),
    ('United States', 'Japan', 'Germany'),
    ('Canada', 'Japan', 'Germany'),
    ('United States', 'India', 'Brazil'),
]
TRADE_TRIOS = [
    ('United States', 'China', 'Japan'),
    ('Germany', 'China', 'France'),
    ('United States', 'Canada', 'Mexico'),
]
CRYPTO_ASSETS = [('Bitcoin', 'Ethereum', 'Solana'), ('Bitcoin', 'Dogecoin', 'Cardano')]
FX_PAIRS = [('USD to EUR', 'USD to GBP', 'USD to JPY'), ('EUR to GBP', 'USD to CAD', 'USD to CHF')]


def annotate(
    session: dict,
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
    session['dataset_tier'] = dataset_tier
    session['evaluation_target'] = certification_target
    session['provenance'] = {
        'snapshot_id': snapshot_id,
        'sampler_version': SAMPLER_VERSION,
        'sampling_probability': sampling_probability,
        'selection_weight': selection_weight,
        'holdout_split': holdout_split,
        'seed': seed,
        'certification_target': certification_target,
        'generation_mode': 'risk_weighted_template_bootstrap',
    }
    for round_case in session.get('rounds') or []:
        if isinstance(round_case, dict):
            gold = round_case.setdefault('gold', {})
            if isinstance(gold, dict):
                gold['evaluation_target'] = certification_target
    return session


def gdp_variant_cycle(idx: int) -> dict:
    a, b, c = GDP_TRIOS[idx % len(GDP_TRIOS)]
    return {
        'id': f'multi-gdp-variant-{idx:06d}',
        'family': 'transform_switch_chain',
        'risk_class': 'high',
        'rounds': [
            {'query': f'{a} GDP', 'gold': {'required_concept_tags': ['gdp'], 'accepted_providers': ['FRED', 'IMF', 'WorldBank', 'Eurostat', 'StatsCan'], 'human_review_required': True}},
            {'query': f'Add {b} GDP', 'gold': {'required_concept_tags': ['gdp'], 'human_review_required': True}},
            {'query': 'Switch to GDP growth rate', 'gold': {'required_concept_tags': ['gdp'], 'required_transform_tags': ['growth'], 'human_review_required': True}},
            {'query': 'Switch to GDP per capita', 'gold': {'required_concept_tags': ['gdp'], 'required_transform_tags': ['per_capita'], 'human_review_required': True}},
            {'query': f'Add {c} back', 'gold': {'required_concept_tags': ['gdp'], 'human_review_required': True}},
        ],
    }


def mixed_provider_switch(idx: int) -> dict:
    a, b, c = GDP_TRIOS[idx % len(GDP_TRIOS)]
    return {
        'id': f'multi-provider-switch-{idx:06d}',
        'family': 'provider_switch_chain',
        'risk_class': 'high',
        'rounds': [
            {'query': f'{a} GDP from FRED', 'gold': {'accepted_providers': ['FRED'], 'required_concept_tags': ['gdp'], 'human_review_required': True}},
            {'query': f'{b} GDP from World Bank', 'gold': {'accepted_providers': ['WORLDBANK'], 'required_concept_tags': ['gdp'], 'human_review_required': True}},
            {'query': f'{c} GDP from IMF', 'gold': {'accepted_providers': ['IMF'], 'required_concept_tags': ['gdp'], 'human_review_required': True}},
            {'query': 'Switch all to GDP growth rate', 'gold': {'required_concept_tags': ['gdp'], 'required_transform_tags': ['growth'], 'human_review_required': True}},
            {'query': 'Change to 2020-2025', 'gold': {'required_concept_tags': ['gdp'], 'required_transform_tags': ['growth'], 'human_review_required': True}},
        ],
    }


def comtrade_bilateral(idx: int) -> dict:
    reporter, partner, add_country = TRADE_TRIOS[idx % len(TRADE_TRIOS)]
    return {
        'id': f'multi-comtrade-{idx:06d}',
        'family': 'comtrade_bilateral_chain',
        'risk_class': 'high',
        'rounds': [
            {'query': f'{reporter} exports to {partner}', 'gold': {'accepted_providers': ['COMTRADE'], 'required_concept_tags': ['export'], 'human_review_required': True}},
            {'query': f'Switch to {reporter} imports from {partner}', 'gold': {'accepted_providers': ['COMTRADE'], 'required_concept_tags': ['import'], 'human_review_required': True}},
            {'query': f'Change partner to {add_country}', 'gold': {'accepted_providers': ['COMTRADE'], 'human_review_required': True}},
            {'query': f'Switch back to {reporter} exports', 'gold': {'accepted_providers': ['COMTRADE'], 'required_concept_tags': ['export'], 'human_review_required': True}},
        ],
    }


def statscan_decomposition(idx: int) -> dict:
    return {
        'id': f'multi-statscan-{idx:06d}',
        'family': 'statscan_decomposition_chain',
        'risk_class': 'high',
        'rounds': [
            {'query': 'Canada unemployment rate', 'gold': {'accepted_providers': ['STATSCAN'], 'required_concept_tags': ['unemployment'], 'human_review_required': True}},
            {'query': 'Show by province', 'gold': {'accepted_providers': ['STATSCAN'], 'required_scope_tags': ['subnational'], 'human_review_required': True}},
            {'query': 'Show only Ontario', 'gold': {'accepted_providers': ['STATSCAN'], 'required_scope_tags': ['subnational'], 'human_review_required': True}},
            {'query': 'Show by age group', 'gold': {'accepted_providers': ['STATSCAN'], 'required_scope_tags': ['subnational'], 'human_review_required': True}},
        ],
    }


def crypto_rotation(idx: int) -> dict:
    a, b, c = CRYPTO_ASSETS[idx % len(CRYPTO_ASSETS)]
    return {
        'id': f'multi-crypto-{idx:06d}',
        'family': 'crypto_rotation_chain',
        'risk_class': 'medium',
        'rounds': [
            {'query': f'{a} price', 'gold': {'accepted_providers': ['COINGECKO'], 'human_review_required': True}},
            {'query': f'Switch to {b} price', 'gold': {'accepted_providers': ['COINGECKO'], 'human_review_required': True}},
            {'query': f'Add {c} for comparison', 'gold': {'accepted_providers': ['COINGECKO'], 'human_review_required': True}},
            {'query': 'Change to last 30 days', 'gold': {'accepted_providers': ['COINGECKO'], 'human_review_required': True}},
        ],
    }


def fx_rotation(idx: int) -> dict:
    a, b, c = FX_PAIRS[idx % len(FX_PAIRS)]
    return {
        'id': f'multi-fx-{idx:06d}',
        'family': 'fx_pair_chain',
        'risk_class': 'medium',
        'rounds': [
            {'query': f'{a} exchange rate', 'gold': {'accepted_providers': ['EXCHANGERATE', 'FRED'], 'human_review_required': True}},
            {'query': f'Switch to {b}', 'gold': {'accepted_providers': ['EXCHANGERATE', 'FRED'], 'human_review_required': True}},
            {'query': f'Switch to {c}', 'gold': {'accepted_providers': ['EXCHANGERATE', 'FRED'], 'human_review_required': True}},
        ],
    }


FAMILY_BUILDERS = {
    'provider_switch_chain': mixed_provider_switch,
    'transform_switch_chain': gdp_variant_cycle,
    'comtrade_bilateral_chain': comtrade_bilateral,
    'statscan_decomposition_chain': statscan_decomposition,
    'crypto_rotation_chain': crypto_rotation,
    'fx_pair_chain': fx_rotation,
}


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate multiround certification candidates from the risk-family plan.')
    parser.add_argument('--strata', type=Path, default=DEFAULT_STRATA)
    parser.add_argument('--snapshot', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--scale', type=float, default=1.0, help='Scale family allocations for demo runs (e.g. 0.01)')
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
    family_alloc = dict(strata['multiround_session_plan']['family_allocation'])
    snapshot_id = f"{snapshot['snapshot_date']}:{str(snapshot['git_sha'])[:8]}:{snapshot['indicator_count']}"
    records = []
    counters = {family: 0 for family in family_alloc}
    for family, count in family_alloc.items():
        builder = FAMILY_BUILDERS[family]
        scaled_count = max(1, round(count * max(args.scale, 0.0))) if args.scale > 0 else 0
        for _ in range(scaled_count):
            counters[family] += 1
            session = builder(counters[family])
            session['id'] = f"{family}-{counters[family]:06d}"
            records.append(
                annotate(
                    session,
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
