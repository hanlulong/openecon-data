#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / 'backend' / 'data' / 'indicators.db'
DEFAULT_OUTPUT = ROOT / 'validation' / 'manifests' / 'strata_definition-v1.json'
DEFAULT_DIRECT_TOTAL = 8000
DEFAULT_PROVIDER_FLOOR = 150

QUERY_FAMILIES = [
    'direct_single_series',
    'direct_multi_country',
    'direct_multi_provider',
    'provider_switch',
    'transform_switch',
    'additive_geography',
    'remove_geography',
    'time_range_change',
    'bilateral_trade',
    'decomposition_subnational',
    'ambiguity_clarification'
]

TRANSFORM_FAMILIES = [
    'level', 'real', 'nominal', 'growth', 'per_capita', 'ppp', 'deflator',
    'ratio_percent_of_gdp', 'imports', 'exports', 'trade_balance',
    'current_account', 'debt_credit', 'rate_yield'
]

SCOPE_FAMILIES = [
    'single_country', 'multi_country', 'bilateral', 'subnational',
    'region_group', 'mixed_provider_scope'
]

AMBIGUITY_CLASSES = [
    'clearly_answerable', 'provider_ambiguous', 'transform_ambiguous',
    'scope_ambiguous', 'decomposition_ambiguous', 'terminology_ambiguous'
]

RISK_WEIGHTED_MULTIROUND_ALLOCATION = {
    'provider_switch_chains': 600,
    'transform_switch_chains': 500,
    'ambiguity_to_clarification_chains': 450,
    'multi_country_add_remove_chains': 400,
    'comtrade_bilateral_chains': 350,
    'statscan_decomposition_chains': 300,
    'crypto_fx_rapid_switch_chains': 200,
    'mixed_provider_stress_chains': 200,
}

AMBIGUITY_ALLOCATION = {
    'transform_ambiguity': 500,
    'provider_ambiguity': 350,
    'scope_ambiguity': 350,
    'decomposition_ambiguity': 300,
    'terminology_ambiguity': 300,
    'dominant_interpretation_cases': 200,
}


def direct_provider_allocation(rows: list[tuple[str, int]], total: int, floor: int) -> dict[str, int]:
    providers = [provider for provider, _ in rows]
    allocation = {provider: floor for provider in providers}
    remaining = total - sum(allocation.values())
    weight_total = sum(count for _, count in rows)
    fractional = []
    for provider, count in rows:
        exact = remaining * (count / weight_total) if weight_total else 0.0
        whole = math.floor(exact)
        allocation[provider] += whole
        fractional.append((exact - whole, provider))
    leftover = total - sum(allocation.values())
    for _, provider in sorted(fractional, reverse=True)[:leftover]:
        allocation[provider] += 1
    return allocation


def main() -> int:
    parser = argparse.ArgumentParser(description='Build validation strata definition and direct-set allocation baseline')
    parser.add_argument('--db-path', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--direct-total', type=int, default=DEFAULT_DIRECT_TOTAL)
    parser.add_argument('--provider-floor', type=int, default=DEFAULT_PROVIDER_FLOOR)
    args = parser.parse_args()

    db_path = args.db_path.resolve()
    if not db_path.exists():
        raise SystemExit(f'Database not found: {db_path}')
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    rows = [(str(provider), int(count)) for provider, count in cur.execute('SELECT provider, COUNT(*) FROM indicators GROUP BY provider ORDER BY COUNT(*) DESC').fetchall()]
    total_indicators = int(cur.execute('SELECT COUNT(*) FROM indicators').fetchone()[0])
    con.close()

    provider_allocation = direct_provider_allocation(rows, args.direct_total, args.provider_floor)

    payload = {
        'version': 1,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'catalog_db_path': str(db_path.relative_to(ROOT)),
        'indicator_count': total_indicators,
        'query_families': QUERY_FAMILIES,
        'transform_families': TRANSFORM_FAMILIES,
        'scope_families': SCOPE_FAMILIES,
        'ambiguity_classes': AMBIGUITY_CLASSES,
        'direct_session_plan': {
            'total_sessions': args.direct_total,
            'provider_min_floor': args.provider_floor,
            'provider_allocation': provider_allocation,
        },
        'multiround_session_plan': {
            'total_sessions': sum(RISK_WEIGHTED_MULTIROUND_ALLOCATION.values()),
            'family_allocation': RISK_WEIGHTED_MULTIROUND_ALLOCATION,
        },
        'ambiguity_session_plan': {
            'total_sessions': sum(AMBIGUITY_ALLOCATION.values()),
            'family_allocation': AMBIGUITY_ALLOCATION,
        },
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
