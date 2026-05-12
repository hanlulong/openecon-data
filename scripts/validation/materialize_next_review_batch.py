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
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    CERTIFICATION_TARGETS,
    DEFAULT_DB,
    certification_target_for_row,
    provider_family_key,
    sample_indicator_rows,
    write_jsonl,
)
from scripts.validation.common import audit_direct_query_shape, direct_query_specificity_score, family_success_adjustment  # noqa: E402
from scripts.validation.sample_direct_cert_set import build_record as build_direct_record  # noqa: E402
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


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def snapshot_id(snapshot: dict) -> str:
    return f"{snapshot['snapshot_date']}:{str(snapshot['git_sha'])[:8]}:{snapshot['indicator_count']}"


def normalize_provider_name(value: str) -> str:
    return str(value or '').strip()


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
            base_count = max(base_count, count * 5, 3_000)
        elif count >= 20:
            base_count = max(base_count, count * 100, 5_000)
        elif count >= 5:
            base_count = max(base_count, count * 500, 5_000)
    elif provider_upper == 'WORLDBANK' and count >= 100:
        base_count = max(base_count, count * 10, count + 1_000)
    return min(provider_population, base_count)


def select_quality_screened_direct_records(records: list[dict], count: int) -> list[dict]:
    def sort_key(record: dict) -> tuple[int, int, int, str]:
        provenance = dict(record.get('provenance') or {})
        origin = dict(record.get('origin') or {})
        risk_level = str(provenance.get('query_quality_risk') or 'low')
        risk_rank = {'low': 0, 'medium': 1, 'high': 2}.get(risk_level, 3)
        reasons = list(provenance.get('query_quality_reasons') or [])
        specificity = direct_query_specificity_score(record)
        popularity = float(origin.get('popularity') or 0.0)
        if certification_target_for_row(record) == CERTIFICATION_TARGET_USER_ANSWERABILITY:
            return (
                1 if risk_rank >= 2 else 0,
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

    ranked = sorted(records, key=sort_key)

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
) -> list[dict]:
    rows = []
    seq = 1
    provider_counts = dict(snapshot_meta.get('provider_counts') or {})
    for target in targets:
        provider = normalize_provider_name(target['name'])
        count = int(target.get('planned_batch_sessions') or 0)
        if count <= 0:
            continue
        provider_population = int(provider_counts.get(provider, count))
        oversample_count = direct_oversample_count(provider, count, provider_population)
        samples = sample_indicator_rows(provider, oversample_count, db_path=db_path.resolve(), seed=seed)
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
            record['id'] = f"batch-direct-{provider.lower()}-{seq:06d}"
            quality = audit_direct_query_shape(record)
            record['provenance']['batch_plan'] = 'next_review_batch'
            record['provenance']['query_quality_risk'] = quality['risk_level']
            record['provenance']['query_quality_reasons'] = quality['reasons']
            candidate_records.append(record)
            seq += 1
        selected_records = select_quality_screened_direct_records(candidate_records, count)
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
) -> list[dict]:
    rows = []
    counters = {name: 0 for name in MULTI_BUILDERS}
    for target in targets:
        family = str(target['name'])
        count = int(target.get('planned_batch_sessions') or 0)
        builder = MULTI_BUILDERS[family]
        counters[family] = max(counters.get(family, 0), int(target.get('current_n') or 0))
        for _ in range(count):
            counters[family] += 1
            session = builder(counters[family])
            session['id'] = f"batch-{family}-{counters[family]:06d}"
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
) -> list[dict]:
    rows = []
    counters = {name: 0 for name in FAMILY_TEMPLATES}
    for target in targets:
        family = str(target['name'])
        count = int(target.get('planned_batch_sessions') or 0)
        templates = FAMILY_TEMPLATES[family]
        total_target = max(int(target.get('target_n') or count), count)
        counters[family] = max(counters.get(family, 0), int(target.get('current_n') or 0))
        for idx in range(count):
            query, behavior, outcomes = templates[counters[family] % len(templates)]
            counters[family] += 1
            rows.append(
                apply_certification_target(
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
            )
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
    args = parser.parse_args()

    batch_plan = load_json(args.batch_plan.resolve())
    snapshot_meta = load_json(args.snapshot.resolve())

    allocation = dict(batch_plan.get('allocation') or {})
    direct_targets = list((allocation.get('direct') or {}).get('targets') or [])
    multiround_targets = list((allocation.get('multiround') or {}).get('targets') or [])
    ambiguity_targets = list((allocation.get('ambiguity') or {}).get('targets') or [])

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    direct_rows = materialize_direct(
        direct_targets,
        snapshot_meta=snapshot_meta,
        seed=args.seed,
        holdout_split=args.holdout_split,
        dataset_tier=args.dataset_tier,
        db_path=args.db_path,
        certification_target=args.certification_target,
    )
    multiround_rows = materialize_multiround(
        multiround_targets,
        snapshot_meta=snapshot_meta,
        seed=args.seed,
        holdout_split=args.holdout_split,
        dataset_tier=args.dataset_tier,
        certification_target=args.certification_target,
    )
    ambiguity_rows = materialize_ambiguity(
        ambiguity_targets,
        snapshot_meta=snapshot_meta,
        seed=args.seed,
        holdout_split=args.holdout_split,
        dataset_tier=args.dataset_tier,
        certification_target=args.certification_target,
    )

    direct_path = output_dir / 'next_batch_direct.jsonl'
    multiround_path = output_dir / 'next_batch_multiround.jsonl'
    ambiguity_path = output_dir / 'next_batch_ambiguity.jsonl'
    all_path = output_dir / 'next_batch_all.jsonl'
    write_jsonl(direct_path, direct_rows)
    write_jsonl(multiround_path, multiround_rows)
    write_jsonl(ambiguity_path, ambiguity_rows)
    write_jsonl(all_path, direct_rows + multiround_rows + ambiguity_rows)

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
            },
            indent=2,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
