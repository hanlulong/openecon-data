#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iter_jsonl(path: Path):
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def normalize_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value if str(item).strip())


COUNTRY_ALIASES = {
    'canada': 'CA',
    'ca': 'CA',
    'china': 'CN',
    'cn': 'CN',
    'de': 'DE',
    'germany': 'DE',
    'jp': 'JP',
    'japan': 'JP',
    'united states': 'US',
    'united states of america': 'US',
    'us': 'US',
    'usa': 'US',
}


def normalize_countries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        normalized.append(COUNTRY_ALIASES.get(text.casefold(), text))
    return sorted(normalized)


def compare_round(local_row: dict[str, Any] | None, production_row: dict[str, Any] | None) -> dict[str, Any]:
    if local_row is None:
        return {
            'status': 'missing_local',
            'difference_types': ['missing_local_round'],
            'local': None,
            'production': production_row,
        }
    if production_row is None:
        return {
            'status': 'missing_production',
            'difference_types': ['missing_production_round'],
            'local': local_row,
            'production': None,
        }

    differences: list[str] = []
    if int(local_row.get('status_code') or 0) != int(production_row.get('status_code') or 0):
        differences.append('status_code_mismatch')
    if bool(local_row.get('clarification_detected')) != bool(production_row.get('clarification_detected')):
        differences.append('clarification_mismatch')
    if int(local_row.get('series_count') or 0) != int(production_row.get('series_count') or 0):
        differences.append('series_count_mismatch')
    if str(local_row.get('error') or '') != str(production_row.get('error') or ''):
        differences.append('error_mismatch')
    if normalize_list(local_row.get('providers')) != normalize_list(production_row.get('providers')):
        differences.append('provider_mismatch')
    if normalize_countries(local_row.get('countries')) != normalize_countries(production_row.get('countries')):
        differences.append('country_mismatch')
    if normalize_list(local_row.get('series_ids')) != normalize_list(production_row.get('series_ids')):
        differences.append('series_id_mismatch')

    return {
        'status': 'match' if not differences else 'different',
        'difference_types': differences,
        'local': local_row,
        'production': production_row,
    }


def difference_severity(difference_types: list[str]) -> str:
    if not difference_types:
        return 'match'
    material_types = {
        'missing_local_round',
        'missing_production_round',
        'round_count_mismatch',
        'status_code_mismatch',
        'clarification_mismatch',
        'error_mismatch',
        'provider_mismatch',
        'country_mismatch',
    }
    if any(item in material_types for item in difference_types):
        return 'material'
    return 'review'


def score_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    metrics = dict(report.get('metrics') or {})
    return {
        'claim_grade_ready': report.get('claim_grade_ready'),
        'scoring_mode': report.get('scoring_mode'),
        'claim_grade_blockers': list(report.get('claim_grade_blockers') or []),
        'claim_observed_success': metrics.get('claim_observed_success'),
        'claim_lower95': metrics.get('claim_lower95'),
        'overall_weighted_provisional_success': metrics.get('overall_weighted_provisional_success'),
        'overall_weighted_lower95_approx': metrics.get('overall_weighted_lower95_approx'),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Compare local and production replay raw outputs and summarize parity drift.')
    parser.add_argument('--local-raw', type=Path, required=True)
    parser.add_argument('--production-raw', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--local-score', type=Path, default=None)
    parser.add_argument('--production-score', type=Path, default=None)
    args = parser.parse_args()

    local_rows = list(iter_jsonl(args.local_raw.resolve()))
    production_rows = list(iter_jsonl(args.production_raw.resolve()))
    local_score = load_json(args.local_score.resolve()) if args.local_score is not None else None
    production_score = load_json(args.production_score.resolve()) if args.production_score is not None else None

    local_by_session: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    production_by_session: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in local_rows:
        local_by_session[str(row.get('session_id') or '')][int(row.get('round_index') or 0)] = row
    for row in production_rows:
        production_by_session[str(row.get('session_id') or '')][int(row.get('round_index') or 0)] = row

    session_ids = sorted(set(local_by_session) | set(production_by_session))
    difference_counts = Counter()
    session_reports: list[dict[str, Any]] = []
    sessions_with_differences = 0
    severity_counts = Counter()

    for session_id in session_ids:
        local_rounds = local_by_session.get(session_id, {})
        production_rounds = production_by_session.get(session_id, {})
        round_indexes = sorted(set(local_rounds) | set(production_rounds))
        round_reports = []
        session_difference_types: set[str] = set()

        if len(local_rounds) != len(production_rounds):
            session_difference_types.add('round_count_mismatch')

        for round_index in round_indexes:
            comparison = compare_round(local_rounds.get(round_index), production_rounds.get(round_index))
            round_severity = difference_severity(comparison['difference_types'])
            round_reports.append(
                {
                    'round_index': round_index,
                    'severity': round_severity,
                    **comparison,
                }
            )
            for difference in comparison['difference_types']:
                difference_counts[difference] += 1
                session_difference_types.add(difference)

        if session_difference_types:
            sessions_with_differences += 1
            if 'round_count_mismatch' in session_difference_types:
                difference_counts['round_count_mismatch'] += 1
        session_severity = difference_severity(sorted(session_difference_types))
        severity_counts[session_severity] += 1

        session_reports.append(
            {
                'session_id': session_id,
                'status': 'match' if not session_difference_types else 'different',
                'severity': session_severity,
                'difference_types': sorted(session_difference_types),
                'round_reports': round_reports,
            }
        )

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'local_raw_path': str(args.local_raw.resolve()),
        'production_raw_path': str(args.production_raw.resolve()),
        'local_score_path': str(args.local_score.resolve()) if args.local_score is not None else None,
        'production_score_path': str(args.production_score.resolve()) if args.production_score is not None else None,
        'summary': {
            'session_count': len(session_ids),
            'sessions_with_differences': sessions_with_differences,
            'difference_counts': dict(difference_counts),
            'severity_counts': dict(severity_counts),
            'local_score': score_summary(local_score),
            'production_score': score_summary(production_score),
        },
        'session_reports': session_reports,
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'sessions_with_differences': sessions_with_differences}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
