#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'certification-raw-results.jsonl'
DEFAULT_BASE_URL = 'http://localhost:3001'


def detect_dataset_type(row: dict[str, Any]) -> str:
    if 'rounds' in row:
        return 'multiround'
    if 'expected_behavior' in row:
        return 'ambiguity'
    return 'direct'


def iter_jsonl(path: Path):
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def dry_run_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type = Counter(detect_dataset_type(row) for row in rows)
    by_tier = Counter(str(row.get('dataset_tier') or '<missing>') for row in rows)
    by_split = Counter(str((row.get('provenance') or {}).get('holdout_split') or '<missing>') for row in rows)
    return {
        'records': len(rows),
        'by_type': dict(by_type),
        'by_tier': dict(by_tier),
        'by_split': dict(by_split),
    }


def detect_clarification(resp_json: dict[str, Any]) -> bool:
    if resp_json.get('clarificationNeeded'):
        return True
    if resp_json.get('clarificationOptions'):
        return True
    if resp_json.get('clarificationQuestions'):
        return True
    error = str(resp_json.get('error') or '')
    if any(word in error.lower() for word in ['clarif', 'ambiguous', 'did you mean']):
        return True
    response_text = str(resp_json.get('response') or '')
    return any(
        phrase in response_text.lower()
        for phrase in ['could you clarify', 'did you mean', 'please specify', 'which specific', 'ambiguous']
    )


def collect_datasets(resp_json: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp_json.get('data')
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        nested = data.get('datasets')
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return [data]
    results = resp_json.get('results')
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    return []


def dataset_has_values(dataset: dict[str, Any]) -> bool:
    for key in ['data', 'values', 'observations', 'time_series', 'timeSeries', 'chart_data', 'chartData']:
        value = dataset.get(key)
        if isinstance(value, list) and len(value) > 0:
            return True
        if isinstance(value, dict) and len(value) > 0:
            return True
    return False


def extract_response_signals(resp_json: dict[str, Any]) -> dict[str, Any]:
    datasets = collect_datasets(resp_json)
    populated_series_count = sum(1 for dataset in datasets if dataset_has_values(dataset))
    if populated_series_count == 0 and datasets:
        populated_series_count = len(datasets)
    providers = set()
    countries = set()
    series_ids = set()
    for dataset in datasets:
        metadata = dataset.get('metadata') or {}
        provider = str(metadata.get('source') or metadata.get('provider') or '').strip()
        if provider:
            providers.add(provider)
        country = str(metadata.get('country') or '').strip()
        if country:
            countries.add(country)
        series_id = str(metadata.get('seriesId') or metadata.get('series_id') or '').strip()
        if series_id:
            series_ids.add(series_id)
    clarification_options = resp_json.get('clarificationOptions') or []
    clarification_questions = resp_json.get('clarificationQuestions') or []
    return {
        'clarification_detected': detect_clarification(resp_json),
        'clarification_options_count': len(clarification_options) if isinstance(clarification_options, list) else 0,
        'clarification_questions_count': len(clarification_questions) if isinstance(clarification_questions, list) else 0,
        'response_text_present': bool(str(resp_json.get('response') or '').strip()),
        'series_count': populated_series_count,
        'providers': sorted(providers),
        'countries': sorted(countries),
        'series_ids': sorted(series_ids),
    }


def execute_rows(
    rows: list[dict[str, Any]],
    base_url: str,
    *,
    progress_output: Path | None = None,
    progress_meta: Path | None = None,
) -> list[dict[str, Any]]:
    base = base_url.rstrip('/') + '/api/query'
    results = []
    total_sessions = len(rows)
    if progress_output is not None and progress_output.exists():
        progress_output.unlink()
    if progress_meta is not None:
        write_progress_summary(
            progress_meta,
            completed_sessions=0,
            total_sessions=total_sessions,
            results_written=0,
            done=False,
            last_session_id=None,
            last_dataset_type=None,
        )
    for session_index, row in enumerate(rows, start=1):
        dataset_type = detect_dataset_type(row)
        if dataset_type == 'multiround':
            conv = None
            for i, round_case in enumerate(row['rounds'], start=1):
                payload = {'query': round_case['query']}
                if conv:
                    payload['conversationId'] = conv
                t0 = time.time()
                resp = requests.post(base, json=payload, timeout=120)
                elapsed = time.time() - t0
                data = resp.json()
                conv = data.get('conversationId') or data.get('conversation_id') or conv
                response_signals = extract_response_signals(data)
                record = {
                    'session_id': row['id'],
                    'dataset_type': dataset_type,
                    'round_index': i,
                    'query': round_case['query'],
                    'status_code': resp.status_code,
                    'elapsed_seconds': round(elapsed, 3),
                    'error': data.get('error'),
                    'series_count': response_signals['series_count'],
                    'providers': response_signals['providers'],
                    'countries': response_signals['countries'],
                    'series_ids': response_signals['series_ids'],
                    'clarification_detected': response_signals['clarification_detected'],
                    'clarification_options_count': response_signals['clarification_options_count'],
                    'clarification_questions_count': response_signals['clarification_questions_count'],
                    'response_text_present': response_signals['response_text_present'],
                }
                results.append(record)
                if progress_output is not None:
                    append_jsonl_row(progress_output, record)
        else:
            payload = {'query': row['query']}
            t0 = time.time()
            resp = requests.post(base, json=payload, timeout=120)
            elapsed = time.time() - t0
            data = resp.json()
            response_signals = extract_response_signals(data)
            record = {
                'session_id': row['id'],
                'dataset_type': dataset_type,
                'round_index': 1,
                'query': row['query'],
                'status_code': resp.status_code,
                'elapsed_seconds': round(elapsed, 3),
                'error': data.get('error'),
                'series_count': response_signals['series_count'],
                'providers': response_signals['providers'],
                'countries': response_signals['countries'],
                'series_ids': response_signals['series_ids'],
                'clarification_detected': response_signals['clarification_detected'],
                'clarification_options_count': response_signals['clarification_options_count'],
                'clarification_questions_count': response_signals['clarification_questions_count'],
                'response_text_present': response_signals['response_text_present'],
            }
            results.append(record)
            if progress_output is not None:
                append_jsonl_row(progress_output, record)
        if progress_meta is not None:
            write_progress_summary(
                progress_meta,
                completed_sessions=session_index,
                total_sessions=total_sessions,
                results_written=len(results),
                done=False,
                last_session_id=str(row.get('id') or ''),
                last_dataset_type=dataset_type,
            )
    if progress_meta is not None:
        write_progress_summary(
            progress_meta,
            completed_sessions=total_sessions,
            total_sessions=total_sessions,
            results_written=len(results),
            done=True,
            last_session_id=str(rows[-1].get('id') or '') if rows else None,
            last_dataset_type=detect_dataset_type(rows[-1]) if rows else None,
        )
    return results


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def append_jsonl_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def progress_output_path(output: Path) -> Path:
    return output.with_name(output.name + '.inprogress')


def progress_meta_path(output: Path) -> Path:
    return output.with_name(output.name + '.progress.json')


def write_progress_summary(
    path: Path,
    *,
    completed_sessions: int,
    total_sessions: int,
    results_written: int,
    done: bool,
    last_session_id: str | None,
    last_dataset_type: str | None,
) -> None:
    payload = {
        'completed_sessions': completed_sessions,
        'total_sessions': total_sessions,
        'results_written': results_written,
        'done': done,
        'last_session_id': last_session_id,
        'last_dataset_type': last_dataset_type,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description='Run or dry-run certification datasets and emit raw execution results.')
    parser.add_argument('--dataset', action='append', type=Path, required=True, help='JSONL dataset file; pass multiple times')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-sessions', type=int, default=None)
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for path in args.dataset:
        rows.extend(list(iter_jsonl(path.resolve())))
    if args.max_sessions is not None:
        rows = rows[:args.max_sessions]

    if args.dry_run:
        summary = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': 'dry_run',
            'summary': dry_run_summary(rows),
            'output': str(args.output.resolve()),
        }
        print(json.dumps(summary, indent=2))
        return 0

    output_path = args.output.resolve()
    results = execute_rows(
        rows,
        args.base_url,
        progress_output=progress_output_path(output_path),
        progress_meta=progress_meta_path(output_path),
    )
    write_jsonl(output_path, results)
    print(json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'execute',
        'records': len(results),
        'output': str(output_path),
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
