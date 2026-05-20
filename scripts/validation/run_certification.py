#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.common import (  # noqa: E402
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    audit_direct_query_shape,
    certification_target_for_row,
    imf_catalog_surface_supportability_reason,
    worldbank_source_id_for_code,
)

DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'certification-raw-results.jsonl'
DEFAULT_BASE_URL = 'http://localhost:3001'


class SessionExecutionError(Exception):
    def __init__(self, failure_record: dict[str, Any], original: Exception):
        super().__init__(str(original))
        self.failure_record = failure_record
        self.original = original


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


def preflight_audit_path(output: Path) -> Path:
    return output.with_name(output.name + '.preflight-audit.json')


def unsupported_direct_surface_reason(row: dict[str, Any], audit: dict[str, Any] | None = None) -> str | None:
    """Return a claim-integrity reason when a direct row is not executable yet.

    Some catalog families are real catalog surface area, but they are not yet
    available through OpenEcon's documented public/runtime provider surface.
    Treating those rows as ordinary direct runtime calls creates misleading
    certification noise; dropping them would hide a weak stratum.  The middle
    path is to keep them in the certification output as explicit failures that
    block the claim until the framework grows true support.
    """
    if detect_dataset_type(row) != 'direct':
        return None

    provenance = row.get('provenance') if isinstance(row.get('provenance'), dict) else {}
    selection_supportability_reason = str(
        provenance.get('selection_supportability_reason') or ''
    ).strip()
    if (
        certification_target_for_row(row) == CERTIFICATION_TARGET_USER_ANSWERABILITY
        and selection_supportability_reason
        and str(provenance.get('supportability_probe_query') or '').strip() == 'imf_exact_provider_code'
    ):
        return selection_supportability_reason

    if certification_target_for_row(row) == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # Real-user certification evaluates the actual user prompt against the
        # live answer path. Legacy catalog-code supportability screens remain
        # useful for inventory replay, but they must not make an answerability
        # run fail before the user query is attempted.
        return None

    origin = dict(row.get('origin') or {})
    provider = str(row.get('provider_stratum') or row.get('provider') or origin.get('source_provider') or '').upper()
    payload = audit if audit is not None else audit_direct_query_shape(row)
    reasons = {str(reason) for reason in payload.get('reasons') or []}
    risk_level = str(payload.get('risk_level') or '').strip().lower()

    if (
        provider == 'OECD'
        and 'oecd_non_production_dataflow' in reasons
        and 'oecd_low_viability_family' in reasons
    ):
        return 'oecd_non_production_dataflow_unsupported'

    category = str(origin.get('category') or row.get('category') or '').strip().lower()

    if provider == 'WORLDBANK' and (
        'worldbank_niche_catalog_family' in reasons
        or 'worldbank_ddh_prevalence_family' in reasons
        or 'worldbank_education_expenditure_family' in reasons
        or 'worldbank_demographic_literacy_slice' in reasons
        or 'worldbank_binary_policy_query' in reasons
        or (risk_level == 'high' and category == 'disability data hub (ddh)')
        or (
            risk_level == 'high'
            and category == 'global financial inclusion and consumer protection survey'
        )
    ):
        return 'worldbank_niche_catalog_unsupported'
    if provider == 'WORLDBANK' and 'worldbank_specialized_source_family' in reasons:
        # Source-specific WorldBank indicators can be publicly executable via
        # /sources/{source}/country/{country}/series/{code}.  Do not preflight
        # block exact codes whose catalog metadata carries a non-WDI source id;
        # let the provider prove or fail the documented source endpoint.
        source_code = str(origin.get('source_indicator_code') or row.get('code') or '').strip()
        source_id = worldbank_source_id_for_code(source_code)
        if source_id and source_id != '2':
            return None
        return 'worldbank_specialized_source_unsupported'
    if provider == 'WORLDBANK' and 'worldbank_country_availability_surface' in reasons:
        return 'worldbank_country_availability_surface'

    if provider != 'IMF':
        return None

    return imf_catalog_surface_supportability_reason(
        str(origin.get('source_indicator_code') or row.get('code') or ''),
        str(origin.get('name') or row.get('name') or ''),
        str(origin.get('category') or row.get('category') or ''),
    )


def preflight_audit_rows(
    rows: list[dict[str, Any]],
    *,
    flagged_limit: int = 50,
    classify_unsupported_direct: bool = False,
) -> dict[str, Any]:
    risk_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    supportability_counts: Counter[str] = Counter()
    flagged_rows: list[dict[str, Any]] = []
    direct_count = 0

    for row in rows:
        kind = detect_dataset_type(row)
        type_counts[kind] += 1
        if kind != 'direct':
            continue
        direct_count += 1
        audit = audit_direct_query_shape(row)
        risk_level = str(audit.get('risk_level') or 'low')
        reasons = [str(reason) for reason in audit.get('reasons') or []]
        supportability_reason = unsupported_direct_surface_reason(row, audit)
        risk_counts[risk_level] += 1
        for reason in reasons:
            reason_counts[reason] += 1
        if supportability_reason:
            supportability_counts[supportability_reason] += 1
        if risk_level != 'low' and len(flagged_rows) < flagged_limit:
            flagged_rows.append({
                'id': row.get('id'),
                'dataset_type': kind,
                'provider_stratum': row.get('provider_stratum') or (row.get('origin') or {}).get('source_provider'),
                'query': row.get('query'),
                'risk_level': risk_level,
                'reasons': reasons,
                'supportability_reason': supportability_reason,
                'execution_mode': (
                    'supportability_blocked'
                    if classify_unsupported_direct and supportability_reason
                    else 'ordinary_runtime'
                ),
                'query_length': audit.get('query_length'),
                'punctuation_hits': audit.get('punctuation_hits'),
            })

    supportability_blocked_rows = sum(supportability_counts.values())
    high_risk_rows = risk_counts.get('high', 0)
    execution_high_risk_rows = (
        max(0, high_risk_rows - supportability_blocked_rows)
        if classify_unsupported_direct
        else high_risk_rows
    )
    return {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'summary': {
            'row_count': len(rows),
            'direct_rows_audited': direct_count,
            'by_type': dict(type_counts),
            'risk_counts': dict(risk_counts),
            'reason_counts': dict(reason_counts),
            'supportability_blocked_rows': supportability_blocked_rows,
            'supportability_blocked_reason_counts': dict(supportability_counts),
            'high_risk_rows': high_risk_rows,
            'execution_high_risk_rows': execution_high_risk_rows,
            'classify_unsupported_direct': classify_unsupported_direct,
        },
        'flagged_rows_sample': flagged_rows,
    }


def write_preflight_audit(path: Path, rows: list[dict[str, Any]], *, classify_unsupported_direct: bool = False) -> dict[str, Any]:
    payload = preflight_audit_rows(rows, classify_unsupported_direct=classify_unsupported_direct)
    atomic_write_json(path, payload)
    return payload


def enforce_preflight_audit(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    allow_high_risk_direct: bool,
    classify_unsupported_direct: bool = False,
) -> dict[str, Any]:
    payload = write_preflight_audit(path, rows, classify_unsupported_direct=classify_unsupported_direct)
    summary = payload.get('summary') or {}
    high_risk_key = 'execution_high_risk_rows' if 'execution_high_risk_rows' in summary else 'high_risk_rows'
    blocking_high_risk_rows = int(summary.get(high_risk_key) or 0)
    if blocking_high_risk_rows and not allow_high_risk_direct:
        raise RuntimeError(
            f'preflight audit blocked certification run: {blocking_high_risk_rows} executable high-risk direct rows; '
            f'regenerate/audit the dataset or pass --allow-high-risk-direct explicitly. audit={path}'
        )
    return payload


def select_rows(
    rows: list[dict[str, Any]],
    *,
    start_index: int = 0,
    max_sessions: int | None = None,
) -> list[dict[str, Any]]:
    if start_index < 0:
        raise ValueError('start_index must be 0 or greater')
    if max_sessions is not None and max_sessions < 0:
        raise ValueError('max_sessions must be 0 or greater')
    selected = rows[start_index:]
    if max_sessions is not None:
        selected = selected[:max_sessions]
    return selected


def validate_unique_session_ids(rows: list[dict[str, Any]]) -> None:
    counts = Counter(str(row.get('id') or '') for row in rows)
    duplicates = sorted(session_id for session_id, count in counts.items() if session_id and count > 1)
    if duplicates:
        preview = ', '.join(duplicates[:5])
        raise ValueError(f'resume/skip-completed requires unique session ids; duplicates: {preview}')


def expected_round_indexes_for_session(row: dict[str, Any]) -> set[int]:
    if detect_dataset_type(row) == 'multiround':
        rounds = row.get('rounds') or []
        if not isinstance(rounds, list):
            return set()
        return set(range(1, len(rounds) + 1))
    return {1}


def completed_session_ids_from_results(
    rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> set[str]:
    expected_by_id = {
        str(row.get('id') or ''): expected_round_indexes_for_session(row)
        for row in rows
        if row.get('id') is not None
    }
    rounds_by_id: dict[str, list[int]] = defaultdict(list)
    for record in records:
        if record.get('request_failed') and not record.get('runtime_unavailable'):
            continue
        session_id = str(record.get('session_id') or '')
        if session_id not in expected_by_id:
            continue
        try:
            round_index = int(record.get('round_index') or 0)
        except (TypeError, ValueError):
            round_index = 0
        rounds_by_id[session_id].append(round_index)

    completed: set[str] = set()
    for session_id, expected_rounds in expected_by_id.items():
        observed_rounds = rounds_by_id.get(session_id, [])
        if expected_rounds and set(observed_rounds) == expected_rounds and len(observed_rounds) == len(expected_rounds):
            completed.add(session_id)
    return completed


def keep_complete_session_records(
    rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    completed_ids = completed_session_ids_from_results(rows, records)
    expected_by_id = {
        str(row.get('id') or ''): expected_round_indexes_for_session(row)
        for row in rows
        if row.get('id') is not None
    }
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for record in records:
        session_id = str(record.get('session_id') or '')
        if session_id not in completed_ids:
            continue
        try:
            round_index = int(record.get('round_index') or 0)
        except (TypeError, ValueError):
            continue
        key = (session_id, round_index)
        if round_index not in expected_by_id.get(session_id, set()) or key in seen:
            continue
        kept.append(record)
        seen.add(key)
    return kept


def load_existing_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    return list(iter_jsonl(path))


def load_resume_records(output_path: Path, progress_output: Path) -> list[dict[str, Any]]:
    progress_records = load_existing_results(progress_output)
    if progress_records:
        return progress_records
    return load_existing_results(output_path)


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


def _dataset_point_dates(dataset: dict[str, Any]) -> list[str]:
    dates: list[str] = []
    for key in ['data', 'values', 'observations', 'time_series', 'timeSeries', 'chart_data', 'chartData']:
        value = dataset.get(key)
        items: list[Any]
        if isinstance(value, list):
            items = value
        elif isinstance(value, dict):
            items = list(value.values())
        else:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_date = (
                item.get('date')
                or item.get('period')
                or item.get('time')
                or item.get('x')
                or item.get('year')
            )
            if raw_date is not None and str(raw_date).strip():
                dates.append(str(raw_date).strip())
    return sorted(dates)


def _dataset_time_range(dataset: dict[str, Any]) -> dict[str, str] | None:
    metadata = dataset.get('metadata') or {}
    start_date = str(
        metadata.get('startDate')
        or metadata.get('start_date')
        or metadata.get('observationStart')
        or metadata.get('observation_start')
        or ''
    ).strip()
    end_date = str(
        metadata.get('endDate')
        or metadata.get('end_date')
        or metadata.get('observationEnd')
        or metadata.get('observation_end')
        or ''
    ).strip()
    if not (start_date and end_date):
        dates = _dataset_point_dates(dataset)
        if dates:
            start_date = start_date or dates[0]
            end_date = end_date or dates[-1]
    if not (start_date or end_date):
        return None
    return {
        'provider': str(metadata.get('source') or metadata.get('provider') or '').strip(),
        'country': str(metadata.get('country') or '').strip(),
        'indicator': str(metadata.get('indicator') or metadata.get('name') or '').strip(),
        'seriesId': str(metadata.get('seriesId') or metadata.get('series_id') or '').strip(),
        'startDate': start_date,
        'endDate': end_date,
    }


def extract_response_signals(resp_json: dict[str, Any]) -> dict[str, Any]:
    datasets = collect_datasets(resp_json)
    populated_series_count = sum(1 for dataset in datasets if dataset_has_values(dataset))
    if populated_series_count == 0 and datasets:
        populated_series_count = len(datasets)
    providers = set()
    countries = set()
    series_ids = set()
    indicators = set()
    api_urls = set()
    source_urls = set()
    dataset_time_ranges: list[dict[str, str]] = []
    for dataset in datasets:
        metadata = dataset.get('metadata') or {}
        provider = str(metadata.get('source') or metadata.get('provider') or '').strip()
        if provider:
            providers.add(provider)
        country = str(metadata.get('country') or '').strip()
        if country:
            countries.add(country)
        indicator = str(metadata.get('indicator') or metadata.get('name') or '').strip()
        if indicator:
            indicators.add(indicator)
        series_id = str(metadata.get('seriesId') or metadata.get('series_id') or '').strip()
        if series_id:
            series_ids.add(series_id)
        api_url = str(metadata.get('apiUrl') or metadata.get('api_url') or '').strip()
        if api_url:
            api_urls.add(api_url)
        source_url = str(metadata.get('sourceUrl') or metadata.get('source_url') or '').strip()
        if source_url:
            source_urls.add(source_url)
        time_range = _dataset_time_range(dataset)
        if time_range:
            dataset_time_ranges.append(time_range)
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
        'indicators': sorted(indicators),
        'api_urls': sorted(api_urls),
        'source_urls': sorted(source_urls),
        'dataset_time_ranges': sorted(
            dataset_time_ranges,
            key=lambda item: (
                item.get('provider', ''),
                item.get('country', ''),
                item.get('seriesId', ''),
                item.get('startDate', ''),
                item.get('endDate', ''),
            ),
        ),
    }


def runtime_supportability_reason(row: dict[str, Any], resp_json: dict[str, Any]) -> str | None:
    provider = str(row.get('provider_stratum') or (row.get('origin') or {}).get('source_provider') or '').upper()
    evidence = ' '.join(
        str(resp_json.get(key) or '')
        for key in ('error', 'message', 'response')
    ).lower()
    if provider == 'COINGECKO' and 'coingecko_price_unavailable' in evidence:
        return 'coingecko_price_unavailable'
    if provider == 'EUROSTAT' and 'eurostat_response_too_large' in evidence:
        return 'eurostat_response_too_large'
    if provider == 'EUROSTAT' and 'eurostat_requested_geo_unavailable' in evidence:
        return 'eurostat_requested_geo_unavailable'
    if provider == 'EUROSTAT' and 'eurostat_dataset_not_disseminated' in evidence:
        return 'eurostat_dataset_not_disseminated'
    if provider == 'IMF' and (
        'imf_non_weo_public_surface_unsupported' in evidence
        or 'imf query targets a detailed imf public-data surface' in evidence
        or 'requires imf dataset-family routing' in evidence
        or 'not yet executable by openecon' in evidence and 'imf dataset-family routing' in evidence
    ):
        return 'imf_non_weo_public_surface_unsupported'
    if provider == 'OECD' and 'oecd_missing_valued_observations' in evidence:
        return 'oecd_missing_valued_observations'
    if provider in {'STATSCAN', 'STATISTICS CANADA'} and 'statscan_required_dimension_missing' in evidence:
        return 'statscan_required_dimension_missing'
    return None


def runtime_unavailable_reason(row: dict[str, Any], resp_json: dict[str, Any]) -> str | None:
    provider = str(row.get('provider_stratum') or (row.get('origin') or {}).get('source_provider') or '').upper()
    evidence = ' '.join(
        str(resp_json.get(key) or '')
        for key in ('error', 'message', 'response')
    ).lower()
    if not evidence:
        return None
    if provider == 'COMTRADE' and (
        'comtrade api quota exhausted' in evidence
        or 'out of call volume quota' in evidence
        or ('comtrade api error' in evidence and 'http 403' in evidence)
    ):
        return 'comtrade_api_quota_or_forbidden'
    if 'rate limit' in evidence or 'too many requests' in evidence or 'quota exhausted' in evidence:
        normalized_provider = (provider or 'provider').lower().replace(' ', '_')
        return f'{normalized_provider}_rate_limited'
    return None


def record_response(row: dict[str, Any], dataset_type: str, round_index: int, query: str, resp, elapsed: float, data: dict[str, Any]) -> dict[str, Any]:
    response_signals = extract_response_signals(data)
    supportability_reason = runtime_supportability_reason(row, data)
    unavailable_reason = None if supportability_reason else runtime_unavailable_reason(row, data)
    message = data.get('message')
    record = {
        'session_id': row['id'],
        'dataset_type': dataset_type,
        'evaluation_target': certification_target_for_row(row),
        'round_index': round_index,
        'query': query,
        'status_code': resp.status_code,
        'elapsed_seconds': round(elapsed, 3),
        'error': data.get('error'),
        'message': message if isinstance(message, str) and message.strip() else None,
        'series_count': response_signals['series_count'],
        'providers': response_signals['providers'],
        'countries': response_signals['countries'],
        'series_ids': response_signals['series_ids'],
        'indicators': response_signals.get('indicators', []),
        'api_urls': response_signals.get('api_urls', []),
        'source_urls': response_signals.get('source_urls', []),
        'dataset_time_ranges': response_signals.get('dataset_time_ranges', []),
        'clarification_detected': response_signals['clarification_detected'],
        'clarification_options_count': response_signals['clarification_options_count'],
        'clarification_questions_count': response_signals['clarification_questions_count'],
        'response_text_present': response_signals['response_text_present'],
    }
    if supportability_reason:
        record.update({
            'supportability_blocked': True,
            'supportability_reason': supportability_reason,
        })
    if unavailable_reason:
        record.update({
            'request_failed': True,
            'runtime_unavailable': True,
            'runtime_unavailable_reason': unavailable_reason,
        })
    return record


def _retry_after_seconds(resp, fallback: float) -> float:
    retry_after = getattr(resp, "headers", {}).get("Retry-After") if getattr(resp, "headers", None) else None
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    return max(0.0, fallback)


def post_query_with_rate_limit_retry(
    base: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    rate_limit_retries: int = 0,
    rate_limit_backoff: float = 10.0,
):
    attempts = 0
    while True:
        resp = requests.post(base, json=payload, timeout=timeout)
        if getattr(resp, "status_code", None) != 429 or attempts >= rate_limit_retries:
            return resp
        attempts += 1
        time.sleep(_retry_after_seconds(resp, rate_limit_backoff * attempts))


def apply_request_spacing(request_spacing: float) -> None:
    if request_spacing > 0:
        time.sleep(request_spacing)


def request_failure_runtime_unavailable_reason(row: dict[str, Any], exc: Exception) -> str | None:
    provider = str(row.get('provider_stratum') or (row.get('origin') or {}).get('source_provider') or '').strip()
    normalized_provider = (provider or 'provider').lower().replace(' ', '_')
    if isinstance(exc, requests.Timeout):
        return f'{normalized_provider}_request_timeout'
    if isinstance(exc, requests.ConnectionError):
        return f'{normalized_provider}_connection_error'
    evidence = str(exc or '').lower()
    if 'read timed out' in evidence or 'connect timeout' in evidence or 'timed out' in evidence:
        return f'{normalized_provider}_request_timeout'
    if 'connection aborted' in evidence or 'connection error' in evidence or 'connection refused' in evidence:
        return f'{normalized_provider}_connection_error'
    return None


def record_failure(row: dict[str, Any], dataset_type: str, round_index: int, query: str, elapsed: float, exc: Exception) -> dict[str, Any]:
    record = {
        'session_id': row.get('id'),
        'dataset_type': dataset_type,
        'evaluation_target': certification_target_for_row(row),
        'round_index': round_index,
        'query': query,
        'status_code': None,
        'elapsed_seconds': round(elapsed, 3),
        'error': str(exc),
        'request_failed': True,
        'series_count': 0,
        'providers': [],
        'countries': [],
        'series_ids': [],
        'clarification_detected': False,
        'clarification_options_count': 0,
        'clarification_questions_count': 0,
        'response_text_present': False,
    }
    unavailable_reason = request_failure_runtime_unavailable_reason(row, exc)
    if unavailable_reason:
        record.update({
            'runtime_unavailable': True,
            'runtime_unavailable_reason': unavailable_reason,
        })
    return record


def record_supportability_blocked(
    row: dict[str, Any],
    dataset_type: str,
    round_index: int,
    query: str,
    audit: dict[str, Any],
    supportability_reason: str,
) -> dict[str, Any]:
    reasons = [str(reason) for reason in audit.get('reasons') or []]
    return {
        'session_id': row.get('id'),
        'dataset_type': dataset_type,
        'evaluation_target': certification_target_for_row(row),
        'round_index': round_index,
        'query': query,
        'status_code': None,
        'elapsed_seconds': 0.0,
        'error': f'supportability_blocked: {supportability_reason}',
        'supportability_blocked': True,
        'supportability_reason': supportability_reason,
        'query_quality_risk': audit.get('risk_level'),
        'query_quality_reasons': reasons,
        'series_count': 0,
        'providers': [],
        'countries': [],
        'series_ids': [],
        'clarification_detected': False,
        'clarification_options_count': 0,
        'clarification_questions_count': 0,
        'response_text_present': False,
    }


def supportability_blocked_record(row: dict[str, Any]) -> dict[str, Any] | None:
    dataset_type = detect_dataset_type(row)
    if dataset_type != 'direct':
        return None
    query = str(row.get('query') or '')
    audit = audit_direct_query_shape(row)
    supportability_reason = unsupported_direct_surface_reason(row, audit)
    if not supportability_reason:
        return None
    return record_supportability_blocked(row, dataset_type, 1, query, audit, supportability_reason)


def record_runtime_unavailable(
    row: dict[str, Any],
    dataset_type: str,
    round_index: int,
    query: str,
    reason: str,
) -> dict[str, Any]:
    return {
        'session_id': row.get('id'),
        'dataset_type': dataset_type,
        'evaluation_target': certification_target_for_row(row),
        'round_index': round_index,
        'query': query,
        'status_code': None,
        'elapsed_seconds': 0.0,
        'error': f'runtime_unavailable: {reason}',
        'request_failed': True,
        'runtime_unavailable': True,
        'runtime_unavailable_reason': reason,
        'series_count': 0,
        'providers': [],
        'countries': [],
        'series_ids': [],
        'clarification_detected': False,
        'clarification_options_count': 0,
        'clarification_questions_count': 0,
        'response_text_present': False,
    }


def runtime_unavailable_records(row: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    dataset_type = detect_dataset_type(row)
    if dataset_type == 'multiround':
        records = []
        for index, round_case in enumerate(row.get('rounds') or [], start=1):
            records.append(
                record_runtime_unavailable(
                    row,
                    dataset_type,
                    index,
                    str((round_case or {}).get('query') or ''),
                    reason,
                )
            )
        return records or [record_runtime_unavailable(row, dataset_type, 1, '', reason)]
    return [record_runtime_unavailable(row, dataset_type, 1, str(row.get('query') or ''), reason)]


def append_failure_checkpoint(
    *,
    row: dict[str, Any],
    dataset_type: str,
    round_index: int,
    query: str,
    elapsed: float,
    exc: Exception,
    results: list[dict[str, Any]],
    progress_output: Path | None,
    progress_meta: Path | None,
    session_index: int,
    total_sessions: int,
    start_index: int,
    skipped_sessions: int,
    completed_session_ids_count: int,
) -> None:
    record = record_failure(row, dataset_type, round_index, query, elapsed, exc)
    results.append(record)
    if progress_output is not None:
        append_jsonl_row(progress_output, record)
    if progress_meta is not None:
        write_progress_summary(
            progress_meta,
            completed_sessions=max(0, session_index - 1),
            total_sessions=total_sessions,
            results_written=len(results),
            done=False,
            last_session_id=str(row.get('id') or ''),
            last_dataset_type=dataset_type,
            start_index=start_index,
            skipped_sessions=skipped_sessions,
            completed_session_ids_count=completed_session_ids_count,
            last_error=str(exc),
        )


def execute_session(
    row: dict[str, Any],
    base: str,
    *,
    classify_unsupported_direct: bool = False,
    request_timeout: float = 120,
    request_spacing: float = 0,
    rate_limit_retries: int = 0,
    rate_limit_backoff: float = 10.0,
    runtime_unavailable_reason: str | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    dataset_type = detect_dataset_type(row)
    if classify_unsupported_direct:
        blocked_record = supportability_blocked_record(row)
        if blocked_record is not None:
            return [blocked_record]
    if runtime_unavailable_reason:
        return runtime_unavailable_records(row, runtime_unavailable_reason)
    if dataset_type == 'multiround':
        conv = None
        for i, round_case in enumerate(row['rounds'], start=1):
            query = round_case['query']
            payload = {'query': query}
            if conv:
                payload['conversationId'] = conv
            t0 = time.time()
            try:
                resp = post_query_with_rate_limit_retry(
                    base,
                    payload,
                    timeout=request_timeout,
                    rate_limit_retries=rate_limit_retries,
                    rate_limit_backoff=rate_limit_backoff,
                )
                apply_request_spacing(request_spacing)
                elapsed = time.time() - t0
                data = resp.json()
            except Exception as exc:
                elapsed = time.time() - t0
                raise SessionExecutionError(record_failure(row, dataset_type, i, query, elapsed, exc), exc) from exc
            conv = data.get('conversationId') or data.get('conversation_id') or conv
            records.append(record_response(row, dataset_type, i, query, resp, elapsed, data))
    else:
        query = row['query']
        t0 = time.time()
        try:
            resp = post_query_with_rate_limit_retry(
                base,
                {'query': query},
                timeout=request_timeout,
                rate_limit_retries=rate_limit_retries,
                rate_limit_backoff=rate_limit_backoff,
            )
            apply_request_spacing(request_spacing)
            elapsed = time.time() - t0
            data = resp.json()
        except Exception as exc:
            elapsed = time.time() - t0
            raise SessionExecutionError(record_failure(row, dataset_type, 1, query, elapsed, exc), exc) from exc
        records.append(record_response(row, dataset_type, 1, query, resp, elapsed, data))
    return records


def execute_rows_concurrent(
    rows: list[dict[str, Any]],
    base_url: str,
    *,
    concurrency: int,
    progress_output: Path | None = None,
    progress_meta: Path | None = None,
    start_index: int = 0,
    skip_completed_session_ids: set[str] | None = None,
    existing_results: list[dict[str, Any]] | None = None,
    preserve_progress_output: bool = False,
    classify_unsupported_direct: bool = False,
    continue_on_error: bool = False,
    request_timeout: float = 120,
    request_spacing: float = 0,
    rate_limit_retries: int = 0,
    rate_limit_backoff: float = 10.0,
    runtime_unavailable_reason: str | None = None,
) -> list[dict[str, Any]]:
    if start_index < 0:
        raise ValueError('start_index must be 0 or greater')
    if concurrency < 1:
        raise ValueError('concurrency must be 1 or greater')
    base = base_url.rstrip('/') + '/api/query'
    results = list(existing_results or [])
    skipped_completed = skip_completed_session_ids or set()
    total_sessions = len(rows)
    skipped_sessions = 0
    runnable: list[tuple[int, dict[str, Any]]] = []

    for session_index, row in enumerate(rows, start=1):
        session_id = str(row.get('id') or '')
        if session_index <= start_index or session_id in skipped_completed:
            skipped_sessions += 1
            continue
        runnable.append((session_index, row))

    if progress_output is not None:
        if progress_output.exists() and not preserve_progress_output:
            progress_output.unlink()
        if preserve_progress_output:
            write_jsonl(progress_output, results)
    if progress_meta is not None:
        write_progress_summary(
            progress_meta,
            completed_sessions=skipped_sessions,
            total_sessions=total_sessions,
            results_written=len(results),
            done=False,
            last_session_id=None,
            last_dataset_type=None,
            start_index=start_index,
            skipped_sessions=skipped_sessions,
            completed_session_ids_count=len(skipped_completed),
            concurrency=concurrency,
        )

    completed_runnable = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                execute_session,
                row,
                base,
                classify_unsupported_direct=classify_unsupported_direct,
                request_timeout=request_timeout,
                request_spacing=request_spacing,
                rate_limit_retries=rate_limit_retries,
                rate_limit_backoff=rate_limit_backoff,
                runtime_unavailable_reason=runtime_unavailable_reason,
            ): (session_index, row)
            for session_index, row in runnable
        }
        for future in as_completed(futures):
            session_index, row = futures[future]
            dataset_type = detect_dataset_type(row)
            session_id = str(row.get('id') or '')
            try:
                session_records = future.result()
            except SessionExecutionError as exc:
                results.append(exc.failure_record)
                if progress_output is not None:
                    append_jsonl_row(progress_output, exc.failure_record)
                completed_runnable += 1
                if progress_meta is not None:
                    write_progress_summary(
                        progress_meta,
                        completed_sessions=skipped_sessions + completed_runnable,
                        total_sessions=total_sessions,
                        results_written=len(results),
                        done=False,
                        last_session_id=session_id,
                        last_dataset_type=dataset_type,
                        start_index=start_index,
                        skipped_sessions=skipped_sessions,
                        completed_session_ids_count=len(skipped_completed),
                        last_error=str(exc.original),
                        concurrency=concurrency,
                    )
                if continue_on_error:
                    continue
                for pending in futures:
                    pending.cancel()
                raise exc.original
            results.extend(session_records)
            if progress_output is not None:
                for record in session_records:
                    append_jsonl_row(progress_output, record)
            completed_runnable += 1
            if progress_meta is not None:
                write_progress_summary(
                    progress_meta,
                    completed_sessions=skipped_sessions + completed_runnable,
                    total_sessions=total_sessions,
                    results_written=len(results),
                    done=False,
                    last_session_id=session_id,
                    last_dataset_type=dataset_type,
                    start_index=start_index,
                    skipped_sessions=skipped_sessions,
                    completed_session_ids_count=len(skipped_completed),
                    concurrency=concurrency,
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
            start_index=start_index,
            skipped_sessions=skipped_sessions,
            completed_session_ids_count=len(skipped_completed),
            concurrency=concurrency,
        )
    return results


def execute_rows_runtime_unavailable(
    rows: list[dict[str, Any]],
    *,
    reason: str,
    progress_output: Path | None = None,
    progress_meta: Path | None = None,
    start_index: int = 0,
    skip_completed_session_ids: set[str] | None = None,
    existing_results: list[dict[str, Any]] | None = None,
    preserve_progress_output: bool = False,
    classify_unsupported_direct: bool = False,
    concurrency: int | None = None,
) -> list[dict[str, Any]]:
    """Generate fail-closed runtime-unavailable rows without per-row HTTP/fsync.

    This path is intentionally only for a proven-unhealthy runtime target.  It
    preserves full certification surface accounting while making every ordinary
    executable session fail closed; unsupported direct rows can still be
    classified separately when requested.
    """
    if start_index < 0:
        raise ValueError('start_index must be 0 or greater')

    results = list(existing_results or [])
    skipped_completed = skip_completed_session_ids or set()
    skipped_sessions = 0
    completed_sessions = 0
    last_session_id: str | None = None
    last_dataset_type: str | None = None

    for session_index, row in enumerate(rows, start=1):
        session_id = str(row.get('id') or '')
        dataset_type = detect_dataset_type(row)
        if session_index <= start_index or session_id in skipped_completed:
            skipped_sessions += 1
            continue
        if classify_unsupported_direct:
            blocked_record = supportability_blocked_record(row)
            if blocked_record is not None:
                session_records = [blocked_record]
            else:
                session_records = runtime_unavailable_records(row, reason)
        else:
            session_records = runtime_unavailable_records(row, reason)
        results.extend(session_records)
        completed_sessions += 1
        last_session_id = session_id
        last_dataset_type = dataset_type

    if progress_output is not None:
        if progress_output.exists() and not preserve_progress_output:
            progress_output.unlink()
        write_jsonl(progress_output, results)
    if progress_meta is not None:
        write_progress_summary(
            progress_meta,
            completed_sessions=skipped_sessions + completed_sessions,
            total_sessions=len(rows),
            results_written=len(results),
            done=True,
            last_session_id=last_session_id,
            last_dataset_type=last_dataset_type,
            start_index=start_index,
            skipped_sessions=skipped_sessions,
            completed_session_ids_count=len(skipped_completed),
            concurrency=concurrency,
        )
    return results


def execute_rows(
    rows: list[dict[str, Any]],
    base_url: str,
    *,
    progress_output: Path | None = None,
    progress_meta: Path | None = None,
    start_index: int = 0,
    skip_completed_session_ids: set[str] | None = None,
    existing_results: list[dict[str, Any]] | None = None,
    preserve_progress_output: bool = False,
    concurrency: int = 1,
    classify_unsupported_direct: bool = False,
    continue_on_error: bool = False,
    request_timeout: float = 120,
    request_spacing: float = 0,
    rate_limit_retries: int = 0,
    rate_limit_backoff: float = 10.0,
    runtime_unavailable_reason: str | None = None,
) -> list[dict[str, Any]]:
    if start_index < 0:
        raise ValueError('start_index must be 0 or greater')
    if concurrency < 1:
        raise ValueError('concurrency must be 1 or greater')
    if runtime_unavailable_reason:
        return execute_rows_runtime_unavailable(
            rows,
            reason=runtime_unavailable_reason,
            progress_output=progress_output,
            progress_meta=progress_meta,
            start_index=start_index,
            skip_completed_session_ids=skip_completed_session_ids,
            existing_results=existing_results,
            preserve_progress_output=preserve_progress_output,
            classify_unsupported_direct=classify_unsupported_direct,
            concurrency=concurrency,
        )
    if concurrency > 1:
        return execute_rows_concurrent(
            rows,
            base_url,
            concurrency=concurrency,
            progress_output=progress_output,
            progress_meta=progress_meta,
            start_index=start_index,
            skip_completed_session_ids=skip_completed_session_ids,
            existing_results=existing_results,
            preserve_progress_output=preserve_progress_output,
            classify_unsupported_direct=classify_unsupported_direct,
            continue_on_error=continue_on_error,
            request_timeout=request_timeout,
            request_spacing=request_spacing,
            rate_limit_retries=rate_limit_retries,
            rate_limit_backoff=rate_limit_backoff,
            runtime_unavailable_reason=runtime_unavailable_reason,
        )
    base = base_url.rstrip('/') + '/api/query'
    results = list(existing_results or [])
    skipped_completed = skip_completed_session_ids or set()
    skipped_sessions = 0
    total_sessions = len(rows)

    if progress_output is not None:
        if progress_output.exists() and not preserve_progress_output:
            progress_output.unlink()
        if preserve_progress_output:
            write_jsonl(progress_output, results)
    if progress_meta is not None:
        write_progress_summary(
            progress_meta,
            completed_sessions=0,
            total_sessions=total_sessions,
            results_written=len(results),
            done=False,
            last_session_id=None,
            last_dataset_type=None,
            start_index=start_index,
            skipped_sessions=0,
            completed_session_ids_count=len(skipped_completed),
        )
    for session_index, row in enumerate(rows, start=1):
        session_id = str(row.get('id') or '')
        dataset_type = detect_dataset_type(row)
        if session_index <= start_index:
            skipped_sessions += 1
            continue
        if session_id in skipped_completed:
            skipped_sessions += 1
            if progress_meta is not None:
                write_progress_summary(
                    progress_meta,
                    completed_sessions=session_index,
                    total_sessions=total_sessions,
                    results_written=len(results),
                    done=False,
                    last_session_id=session_id,
                    last_dataset_type=dataset_type,
                    start_index=start_index,
                    skipped_sessions=skipped_sessions,
                    completed_session_ids_count=len(skipped_completed),
                )
            continue
        if classify_unsupported_direct:
            blocked_record = supportability_blocked_record(row)
            if blocked_record is not None:
                results.append(blocked_record)
                if progress_output is not None:
                    append_jsonl_row(progress_output, blocked_record)
                if progress_meta is not None:
                    write_progress_summary(
                        progress_meta,
                        completed_sessions=session_index,
                        total_sessions=total_sessions,
                        results_written=len(results),
                        done=False,
                        last_session_id=str(row.get('id') or ''),
                        last_dataset_type=dataset_type,
                        start_index=start_index,
                        skipped_sessions=skipped_sessions,
                        completed_session_ids_count=len(skipped_completed),
                    )
                continue
        if runtime_unavailable_reason:
            failure_records = runtime_unavailable_records(row, runtime_unavailable_reason)
            results.extend(failure_records)
            if progress_output is not None:
                for record in failure_records:
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
                    start_index=start_index,
                    skipped_sessions=skipped_sessions,
                    completed_session_ids_count=len(skipped_completed),
                )
            continue
        if dataset_type == 'multiround':
            conv = None
            for i, round_case in enumerate(row['rounds'], start=1):
                query = round_case['query']
                payload = {'query': query}
                if conv:
                    payload['conversationId'] = conv
                t0 = time.time()
                try:
                    resp = post_query_with_rate_limit_retry(
                        base,
                        payload,
                        timeout=request_timeout,
                        rate_limit_retries=rate_limit_retries,
                        rate_limit_backoff=rate_limit_backoff,
                    )
                    apply_request_spacing(request_spacing)
                    elapsed = time.time() - t0
                    data = resp.json()
                except Exception as exc:
                    elapsed = time.time() - t0
                    append_failure_checkpoint(
                        row=row,
                        dataset_type=dataset_type,
                        round_index=i,
                        query=query,
                        elapsed=elapsed,
                        exc=exc,
                        results=results,
                        progress_output=progress_output,
                        progress_meta=progress_meta,
                        session_index=session_index,
                        total_sessions=total_sessions,
                        start_index=start_index,
                        skipped_sessions=skipped_sessions,
                        completed_session_ids_count=len(skipped_completed),
                    )
                    if continue_on_error:
                        break
                    raise
                conv = data.get('conversationId') or data.get('conversation_id') or conv
                record = record_response(row, dataset_type, i, query, resp, elapsed, data)
                results.append(record)
                if progress_output is not None:
                    append_jsonl_row(progress_output, record)
        else:
            query = row['query']
            payload = {'query': query}
            t0 = time.time()
            try:
                resp = post_query_with_rate_limit_retry(
                    base,
                    payload,
                    timeout=request_timeout,
                    rate_limit_retries=rate_limit_retries,
                    rate_limit_backoff=rate_limit_backoff,
                )
                apply_request_spacing(request_spacing)
                elapsed = time.time() - t0
                data = resp.json()
            except Exception as exc:
                elapsed = time.time() - t0
                append_failure_checkpoint(
                    row=row,
                    dataset_type=dataset_type,
                    round_index=1,
                    query=query,
                    elapsed=elapsed,
                    exc=exc,
                    results=results,
                    progress_output=progress_output,
                    progress_meta=progress_meta,
                    session_index=session_index,
                    total_sessions=total_sessions,
                    start_index=start_index,
                    skipped_sessions=skipped_sessions,
                    completed_session_ids_count=len(skipped_completed),
                )
                if continue_on_error:
                    continue
                raise
            record = record_response(row, dataset_type, 1, query, resp, elapsed, data)
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
                start_index=start_index,
                skipped_sessions=skipped_sessions,
                completed_session_ids_count=len(skipped_completed),
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
            start_index=start_index,
            skipped_sessions=skipped_sessions,
            completed_session_ids_count=len(skipped_completed),
        )
    return results


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def append_jsonl_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def progress_output_path(output: Path) -> Path:
    return output.with_name(output.name + '.inprogress')


def progress_meta_path(output: Path) -> Path:
    return output.with_name(output.name + '.progress.json')


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        f.write(json.dumps(payload, indent=2) + '\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def write_progress_summary(
    path: Path,
    *,
    completed_sessions: int,
    total_sessions: int,
    results_written: int,
    done: bool,
    last_session_id: str | None,
    last_dataset_type: str | None,
    start_index: int | None = None,
    skipped_sessions: int | None = None,
    completed_session_ids_count: int | None = None,
    last_error: str | None = None,
    concurrency: int | None = None,
) -> None:
    payload = {
        'updated_at_utc': datetime.now(timezone.utc).isoformat(),
        'completed_sessions': completed_sessions,
        'total_sessions': total_sessions,
        'results_written': results_written,
        'done': done,
        'last_session_id': last_session_id,
        'last_dataset_type': last_dataset_type,
    }
    if start_index is not None:
        payload['start_index'] = start_index
    if skipped_sessions is not None:
        payload['skipped_sessions'] = skipped_sessions
    if completed_session_ids_count is not None:
        payload['completed_session_ids_count'] = completed_session_ids_count
    if last_error is not None:
        payload['last_error'] = last_error
    if concurrency is not None:
        payload['concurrency'] = concurrency
    atomic_write_json(path, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description='Run or dry-run certification datasets and emit raw execution results.')
    parser.add_argument('--dataset', action='append', type=Path, required=True, help='JSONL dataset file; pass multiple times')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-sessions', type=int, default=None)
    parser.add_argument('--start-index', type=int, default=0, help='0-based session index to start from before applying --max-sessions.')
    parser.add_argument('--concurrency', type=int, default=1, help='Number of certification sessions to execute concurrently; multiround sessions remain ordered internally.')
    parser.add_argument('--request-timeout', type=float, default=120, help='Per-round HTTP request timeout in seconds.')
    parser.add_argument('--request-spacing', type=float, default=0, help='Seconds to sleep after each HTTP request; useful for production replay rate limits.')
    parser.add_argument('--rate-limit-retries', type=int, default=0, help='Number of HTTP 429 retries per request.')
    parser.add_argument('--rate-limit-backoff', type=float, default=10.0, help='Fallback seconds for HTTP 429 retry backoff when Retry-After is absent.')
    parser.add_argument('--resume', action='store_true', help='Load existing .inprogress or final output and skip completed sessions.')
    parser.add_argument('--skip-completed', action='store_true', help='Skip sessions already complete in existing .inprogress or final output.')
    parser.add_argument('--preflight-audit-output', type=Path, default=None, help='Path for current direct-query audit gate output; defaults beside --output.')
    parser.add_argument('--allow-high-risk-direct', action='store_true', help='Allow execution even when current preflight audit finds high-risk direct rows.')
    parser.add_argument(
        '--classify-unsupported-direct',
        action='store_true',
        help=(
            'Keep currently unsupported public-surface direct rows in the run as synthetic '
            'supportability-blocked failures instead of sending them to runtime. This does '
            'not count as success and still blocks claim readiness.'
        ),
    )
    parser.add_argument(
        '--continue-on-error',
        action='store_true',
        help='Record request exceptions as failed rows and continue the baseline instead of aborting at the first transport failure.',
    )
    parser.add_argument(
        '--runtime-unavailable-reason',
        default=None,
        help=(
            'Fail closed without HTTP calls by writing runtime_unavailable failure rows for ordinary executable sessions. '
            'Use only after an external health probe shows the runtime target is unavailable or too unhealthy for baseline execution.'
        ),
    )
    parser.add_argument('--skip-preflight-audit', action='store_true', help='Skip the current direct-query audit gate before execution.')
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for path in args.dataset:
        rows.extend(list(iter_jsonl(path.resolve())))
    selected_rows = select_rows(rows, start_index=args.start_index, max_sessions=args.max_sessions)

    if args.dry_run:
        preflight = None if args.skip_preflight_audit else preflight_audit_rows(
            selected_rows,
            classify_unsupported_direct=args.classify_unsupported_direct,
        )
        summary = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'mode': 'dry_run',
            'summary': dry_run_summary(selected_rows),
            'preflight_audit': preflight,
            'total_input_records': len(rows),
            'start_index': args.start_index,
            'max_sessions': args.max_sessions,
            'concurrency': args.concurrency,
            'request_timeout': args.request_timeout,
            'request_spacing': args.request_spacing,
            'rate_limit_retries': args.rate_limit_retries,
            'rate_limit_backoff': args.rate_limit_backoff,
            'resume': args.resume,
            'skip_completed': args.skip_completed,
            'classify_unsupported_direct': args.classify_unsupported_direct,
            'continue_on_error': args.continue_on_error,
            'runtime_unavailable_reason': args.runtime_unavailable_reason,
            'output': str(args.output.resolve()),
            'progress_output': str(progress_output_path(args.output.resolve())),
            'progress_meta': str(progress_meta_path(args.output.resolve())),
        }
        print(json.dumps(summary, indent=2))
        return 0

    output_path = args.output.resolve()
    if not args.skip_preflight_audit:
        audit_output = args.preflight_audit_output.resolve() if args.preflight_audit_output else preflight_audit_path(output_path)
        try:
            enforce_preflight_audit(
                audit_output,
                selected_rows,
                allow_high_risk_direct=args.allow_high_risk_direct,
                classify_unsupported_direct=args.classify_unsupported_direct,
            )
        except RuntimeError as exc:
            print(json.dumps({
                'generated_at_utc': datetime.now(timezone.utc).isoformat(),
                'mode': 'preflight_blocked',
                'error': str(exc),
                'audit_output': str(audit_output),
            }, indent=2), file=sys.stderr)
            return 2
    progress_output = progress_output_path(output_path)
    progress_meta = progress_meta_path(output_path)
    existing_records: list[dict[str, Any]] = []
    completed_session_ids: set[str] = set()
    if args.resume or args.skip_completed:
        validate_unique_session_ids(selected_rows)
        existing_records = load_resume_records(output_path, progress_output)
        existing_records = keep_complete_session_records(selected_rows, existing_records)
        completed_session_ids = completed_session_ids_from_results(selected_rows, existing_records)
    results = execute_rows(
        selected_rows,
        args.base_url,
        progress_output=progress_output,
        progress_meta=progress_meta,
        start_index=0,
        skip_completed_session_ids=completed_session_ids,
        existing_results=existing_records,
        preserve_progress_output=args.resume or args.skip_completed,
        concurrency=args.concurrency,
        classify_unsupported_direct=args.classify_unsupported_direct,
        continue_on_error=args.continue_on_error,
        request_timeout=args.request_timeout,
        request_spacing=args.request_spacing,
        rate_limit_retries=args.rate_limit_retries,
        rate_limit_backoff=args.rate_limit_backoff,
        runtime_unavailable_reason=args.runtime_unavailable_reason,
    )
    write_jsonl(output_path, results)
    print(json.dumps({
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'execute',
        'records': len(results),
        'selected_sessions': len(selected_rows),
        'start_index': args.start_index,
        'skipped_completed_sessions': len(completed_session_ids),
        'classify_unsupported_direct': args.classify_unsupported_direct,
        'continue_on_error': args.continue_on_error,
        'request_timeout': args.request_timeout,
        'request_spacing': args.request_spacing,
        'rate_limit_retries': args.rate_limit_retries,
        'rate_limit_backoff': args.rate_limit_backoff,
        'runtime_unavailable_reason': args.runtime_unavailable_reason,
        'output': str(output_path),
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
