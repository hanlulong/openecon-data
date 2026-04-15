#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'certification_gap_report.json'
DEFAULT_MAX_EFFECTIVE_N = 5000


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)) / denom
    return center - margin


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def min_effective_n_for(lower95_target: float, observed_success: float, *, max_effective_n: int) -> int | None:
    bounded_success = min(max(float(observed_success), 0.0), 1.0)
    for n in range(1, max_effective_n + 1):
        successes = min(n, max(0, round(bounded_success * n)))
        if wilson_lower(successes, n) >= lower95_target:
            return n
    return None


def additional_needed(required_n: int | None, current_n: float | None) -> int | None:
    if required_n is None or current_n is None:
        return None
    return max(0, required_n - int(math.ceil(current_n)))


def scenario_table(
    *,
    lower95_target: float,
    current_observed_success: float,
    max_effective_n: int,
) -> list[dict[str, Any]]:
    scenarios = []
    seen: set[float] = set()
    for observed_success in [
        current_observed_success,
        0.995,
        0.997,
        0.999,
        1.0,
    ]:
        rounded = round(observed_success, 6)
        if rounded in seen:
            continue
        seen.add(rounded)
        required_n = min_effective_n_for(lower95_target, observed_success, max_effective_n=max_effective_n)
        scenarios.append(
            {
                'observed_success': observed_success,
                'required_effective_n': required_n,
                'lower95_at_required_n': (
                    wilson_lower(round(observed_success * required_n), required_n)
                    if required_n is not None
                    else None
                ),
            }
        )
    return scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description='Estimate the certification sample-size gap needed to clear the lower95 claim threshold.')
    parser.add_argument('--score-report', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--required-lower95', type=float, default=None)
    parser.add_argument('--max-effective-n', type=int, default=DEFAULT_MAX_EFFECTIVE_N)
    args = parser.parse_args()

    report = load_json(args.score_report.resolve())
    metrics = dict(report.get('metrics') or {})
    floor_policy = load_json(Path(report['floor_policy_path']).resolve()) if report.get('floor_policy_path') else {}
    claim_thresholds = dict((floor_policy or {}).get('claim_thresholds') or {})

    current_observed_success = float(metrics.get('claim_observed_success') or metrics.get('overall_weighted_provisional_success') or 0.0)
    current_lower95 = float(metrics.get('claim_lower95') or metrics.get('overall_weighted_lower95_approx') or 0.0)
    current_effective_n = metrics.get('overall_weighted_effective_n')
    required_lower95 = (
        float(args.required_lower95)
        if args.required_lower95 is not None
        else float(claim_thresholds.get('lower95_min', 0.99))
    )

    required_n_at_current_success = min_effective_n_for(required_lower95, current_observed_success, max_effective_n=args.max_effective_n)
    required_n_at_perfect_success = min_effective_n_for(required_lower95, 1.0, max_effective_n=args.max_effective_n)

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'score_report_path': str(args.score_report.resolve()),
        'snapshot_id': report.get('snapshot_id'),
        'required_lower95': required_lower95,
        'current': {
            'claim_observed_success': current_observed_success,
            'claim_lower95': current_lower95,
            'overall_weighted_effective_n': current_effective_n,
        },
        'gap_estimate': {
            'required_effective_n_at_current_success': required_n_at_current_success,
            'additional_effective_n_needed_at_current_success': additional_needed(required_n_at_current_success, current_effective_n),
            'required_effective_n_at_perfect_success': required_n_at_perfect_success,
            'additional_effective_n_needed_at_perfect_success': additional_needed(required_n_at_perfect_success, current_effective_n),
        },
        'scenarios': scenario_table(
            lower95_target=required_lower95,
            current_observed_success=current_observed_success,
            max_effective_n=args.max_effective_n,
        ),
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(
        json.dumps(
            {
                'output': str(output),
                'required_effective_n_at_current_success': required_n_at_current_success,
                'required_effective_n_at_perfect_success': required_n_at_perfect_success,
            },
            indent=2,
        )
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
