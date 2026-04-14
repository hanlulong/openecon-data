#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'claim_decision.json'


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)) / denom
    return center - margin


def main() -> int:
    parser = argparse.ArgumentParser(description='Apply a conservative claim gate to a certification score report.')
    parser.add_argument('--score-report', type=Path, required=True)
    parser.add_argument('--adjudication-summary', type=Path, default=None)
    parser.add_argument('--production-score-report', type=Path, default=None)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--required-observed', type=float, default=0.992)
    parser.add_argument('--required-lower95', type=float, default=0.99)
    args = parser.parse_args()

    report = json.loads(args.score_report.resolve().read_text(encoding='utf-8'))
    adjudication_summary = None
    if args.adjudication_summary is not None and args.adjudication_summary.exists():
        adjudication_summary = json.loads(args.adjudication_summary.resolve().read_text(encoding='utf-8'))
    production_score_report = None
    if args.production_score_report is not None and args.production_score_report.exists():
        production_score_report = json.loads(args.production_score_report.resolve().read_text(encoding='utf-8'))
    session_results = list(report.get('session_results') or [])
    successes = sum(1 for row in session_results if row.get('provisional_structural_pass'))
    total = len(session_results)
    observed = successes / total if total else 0.0
    lower95 = wilson_lower(successes, total)

    blockers = []
    if report.get('scoring_mode') != 'claim_grade':
        blockers.append('scoring_mode is not claim_grade')
    if not report.get('claim_grade_ready', False):
        blockers.append('score report is not marked claim_grade_ready')
    if observed < args.required_observed:
        blockers.append(f'observed success {observed:.6f} is below required {args.required_observed:.6f}')
    if lower95 < args.required_lower95:
        blockers.append(f'lower95 {lower95:.6f} is below required {args.required_lower95:.6f}')
    if adjudication_summary is None:
        blockers.append('adjudication summary missing')
    elif not adjudication_summary.get('adjudication_complete', False):
        blockers.append('adjudication is not complete')
    if production_score_report is None:
        blockers.append('production score report missing')
    elif not production_score_report.get('claim_grade_ready', False):
        blockers.append('production score report is not marked claim_grade_ready')

    decision = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'score_report': str(args.score_report.resolve()),
        'adjudication_summary': str(args.adjudication_summary.resolve()) if args.adjudication_summary else None,
        'production_score_report': str(args.production_score_report.resolve()) if args.production_score_report else None,
        'observed_success': observed,
        'lower95': lower95,
        'required_observed': args.required_observed,
        'required_lower95': args.required_lower95,
        'claim_allowed': len(blockers) == 0,
        'blockers': blockers,
        'note': 'This gate is intentionally conservative and will refuse a catalog-wide claim until claim-grade scoring and adjudication are in place.',
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(decision, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(decision, indent=2))
    return 0 if decision['claim_allowed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
