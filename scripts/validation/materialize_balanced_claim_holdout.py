#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation.build_review_expansion_plan import required_groups  # noqa: E402
from scripts.validation.common import (  # noqa: E402
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
    DEFAULT_DB,
    audit_direct_query_shape,
    default_query_for_row,
    sample_indicator_rows,
    selection_supportability_reason_for_row,
    write_jsonl,
)
from backend.utils.coingecko_supportability import (  # noqa: E402
    coingecko_catalog_price_supportability_reason,
)
from scripts.validation.materialize_next_review_batch import (  # noqa: E402
    MULTI_BUILDERS,
    apply_certification_target,
    prefixed_session_id,
    snapshot_id,
)
from scripts.validation.sample_ambiguity_cert_set import FAMILY_TEMPLATES, make_record as make_ambiguity_record  # noqa: E402
from scripts.validation.sample_direct_cert_set import build_record as build_direct_record  # noqa: E402
from scripts.validation.sample_multiround_cert_set import annotate as annotate_multiround  # noqa: E402

DEFAULT_SNAPSHOT = ROOT / 'validation' / 'manifests' / 'catalog_snapshot-2026-04-14.json'
DEFAULT_FLOOR_POLICY = ROOT / 'validation' / 'manifests' / 'claim_gate_policy-v1.json'
COINGECKO_SIMPLE_PRICE_BATCH_SIZE = 200
COMTRADE_PERIOD_CHUNK_SIZE = 12
COMTRADE_SUPPORTABILITY_REASON = 'comtrade_hs_reporter_no_observations'
COMTRADE_SUPPORTABILITY_UNPROBED_REASON = 'comtrade_supportability_probe_scope_exhausted'
COMTRADE_SUPPORTABILITY_REQUEST_SPACING_SECONDS = 1.25
COMTRADE_SUPPORTABILITY_ENDPOINT = 'https://comtradeapi.un.org/data/v1/get/C/A/HS'
COMTRADE_SUPPORTABILITY_PARTNER_CODE = '0'
COMTRADE_SUPPORTABILITY_FREQ_CODE = 'A'
COMTRADE_SUPPORTABILITY_FLOW_CODES = ('X', 'M,X')
COMTRADE_SUPPORTABILITY_PROBE_CONTRACT = (
    'reporterCode + partnerCode=0 + cmdCode + flowCode=X then M,X + freqCode=A + annual periods'
)
COMTRADE_SUPPORTABILITY_ARTIFACT_VERSION = 2
_COMTRADE_SUPPORTABILITY_LAST_REQUEST_STARTED = 0.0


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def provider_catalog_count(provider: str, db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    count = con.execute('SELECT COUNT(*) FROM indicators WHERE provider = ?', (provider,)).fetchone()[0]
    con.close()
    return int(count or 0)


def _coingecko_slug_for_price_probe(row: dict[str, Any]) -> str:
    code = str(row.get('code') or '').strip().lower()
    if re.fullmatch(r'[a-z0-9][a-z0-9\-]{1,127}', code):
        return code
    return ''


def _fetch_coingecko_simple_price_payload(
    ids: list[str],
    *,
    vs_currency: str = 'usd',
    timeout: float = 20.0,
    max_retries: int = 3,
) -> dict[str, Any]:
    if not ids:
        return {}
    query = urllib.parse.urlencode({
        'ids': ','.join(ids),
        'vs_currencies': vs_currency,
    })
    url = f'https://api.coingecko.com/api/v3/simple/price?{query}'
    request = urllib.request.Request(url, headers={'User-Agent': 'OpenEcon validation supportability probe'})
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - provider public API probe
                payload = json.loads(response.read().decode('utf-8'))
            return payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= max_retries - 1:
                raise
            retry_after = exc.headers.get('Retry-After')
            try:
                delay = float(retry_after) if retry_after else 15.0 * (attempt + 1)
            except ValueError:
                delay = 15.0 * (attempt + 1)
            time.sleep(min(delay, 45.0))
    return {}


def coingecko_price_unavailability_by_code(
    rows: list[dict[str, Any]],
    *,
    vs_currency: str = 'usd',
    batch_size: int = COINGECKO_SIMPLE_PRICE_BATCH_SIZE,
    artifact_path: Path | None = None,
    max_slugs: int | None = None,
) -> dict[str, str]:
    """Return CoinGecko rows whose current-price metric is absent upstream.

    This provider-contract probe uses exact catalog slugs against CoinGecko's
    documented current-price endpoint.  It is intentionally materializer-only
    supportability inventory and never marks a row as passing.
    """
    slugs = list(dict.fromkeys(
        slug
        for row in rows
        for slug in [_coingecko_slug_for_price_probe(row)]
        if slug
    ))
    if max_slugs is not None:
        slugs = slugs[:max(0, max_slugs)]
    unavailable: dict[str, str] = {}
    artifact_batches: list[dict[str, Any]] = []
    for start in range(0, len(slugs), batch_size):
        batch = slugs[start : start + batch_size]
        try:
            payload = _fetch_coingecko_simple_price_payload(batch, vs_currency=vs_currency)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                'CoinGecko current-price supportability probe failed; '
                'rerun when provider contract is reachable or disable the probe explicitly'
            ) from exc
        payload_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        batch_unavailable: list[str] = []
        for slug in batch:
            reason = coingecko_catalog_price_supportability_reason(
                code=slug,
                simple_price_payload=payload,
                vs_currency=vs_currency,
            )
            if reason:
                unavailable[slug] = reason
                batch_unavailable.append(slug)
        artifact_batches.append({
            'batch_start': start,
            'requested_slug_count': len(batch),
            'response_slug_count': len(payload),
            'unavailable_slug_count': len(batch_unavailable),
            'unavailable_slugs': batch_unavailable,
            'response_sha256': hashlib.sha256(payload_json.encode('utf-8')).hexdigest(),
        })
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'provider': 'CoinGecko',
            'endpoint': 'https://api.coingecko.com/api/v3/simple/price',
            'metric': vs_currency,
            'reason': 'coingecko_current_price_unavailable',
            'requested_slug_count': len(slugs),
            'probe_scope': 'selection_prefix',
            'unavailable_slug_count': len(unavailable),
            'unavailable_slugs': sorted(unavailable),
            'batch_size': batch_size,
            'batches': artifact_batches,
        }
        artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return unavailable


def _load_env_value(name: str) -> str:
    value = str(os.environ.get(name) or '').strip()
    if value:
        return value
    env_path = ROOT / '.env'
    if not env_path.exists():
        return ''
    try:
        for line in env_path.read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            key, raw_value = stripped.split('=', 1)
            if key.strip() == name:
                return raw_value.strip().strip('"').strip("'")
    except OSError:
        return ''
    return ''


def _comtrade_country_code(country: str) -> str:
    try:
        from backend.providers.comtrade_metadata import get_country_code  # noqa: WPS433 - materializer helper

        return str(get_country_code(country))
    except Exception:
        return ''


def _comtrade_probe_key(row: dict[str, Any]) -> str:
    query = default_query_for_row(
        row,
        certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
    )
    reporter = query.split(' exports of ', 1)[0].strip()
    reporter_code = _comtrade_country_code(reporter)
    hs_code = re.sub(r'^HS', '', str(row.get('code') or '').strip().upper()).strip()
    if not reporter_code or not re.fullmatch(r'\d{2,6}', hs_code):
        return ''
    return f'{reporter_code}:{hs_code}'


def _comtrade_period_chunks(*, start_year: int = 2002, end_year: int | None = None) -> list[str]:
    if end_year is None:
        end_year = datetime.now(timezone.utc).year - 2
    if end_year < start_year:
        end_year = start_year
    years = [str(year) for year in range(start_year, end_year + 1)]
    return [
        ','.join(years[index : index + COMTRADE_PERIOD_CHUNK_SIZE])
        for index in range(0, len(years), COMTRADE_PERIOD_CHUNK_SIZE)
    ]


def _fetch_comtrade_payload(
    *,
    reporter_code: str,
    cmd_code: str,
    period: str,
    flow_code: str,
    api_key: str,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> dict[str, Any]:
    global _COMTRADE_SUPPORTABILITY_LAST_REQUEST_STARTED
    query = {
        'typeCode': 'C',
        'freqCode': COMTRADE_SUPPORTABILITY_FREQ_CODE,
        'clCode': 'HS',
        'reporterCode': reporter_code,
        'period': period,
        'partnerCode': COMTRADE_SUPPORTABILITY_PARTNER_CODE,
        'cmdCode': cmd_code,
        'flowCode': flow_code,
        'format': 'json',
    }
    if api_key:
        query['subscription-key'] = api_key
    url = f'{COMTRADE_SUPPORTABILITY_ENDPOINT}?' + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={'User-Agent': 'OpenEcon validation Comtrade supportability probe'},
    )
    for attempt in range(max_retries):
        try:
            elapsed = time.monotonic() - _COMTRADE_SUPPORTABILITY_LAST_REQUEST_STARTED
            if elapsed < COMTRADE_SUPPORTABILITY_REQUEST_SPACING_SECONDS:
                time.sleep(COMTRADE_SUPPORTABILITY_REQUEST_SPACING_SECONDS - elapsed)
            _COMTRADE_SUPPORTABILITY_LAST_REQUEST_STARTED = time.monotonic()
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - provider public API probe
                payload = json.loads(response.read().decode('utf-8'))
            return payload if isinstance(payload, dict) else {}
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= max_retries - 1:
                raise
            retry_after = exc.headers.get('Retry-After')
            try:
                delay = float(retry_after) if retry_after else 10.0 * (attempt + 1)
            except ValueError:
                delay = 10.0 * (attempt + 1)
            time.sleep(min(delay, 45.0))
    return {}


def _comtrade_payload_has_export_rows(payload: dict[str, Any]) -> bool:
    rows = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        flow_code = str(row.get('flowCode') or row.get('flowCodeOriginal') or '').upper()
        flow_desc = str(row.get('flowDesc') or row.get('flow') or '').lower()
        if flow_code == 'X' or flow_desc.startswith('export'):
            return True
    return False


def _comtrade_export_observations_available(
    *,
    reporter_code: str,
    cmd_code: str,
    api_key: str,
    period_chunks: list[str],
) -> bool:
    for period in period_chunks:
        payload = _fetch_comtrade_payload(
            reporter_code=reporter_code,
            cmd_code=cmd_code,
            period=period,
            flow_code='X',
            api_key=api_key,
        )
        if _comtrade_payload_has_export_rows(payload):
            return True
    for period in period_chunks:
        payload = _fetch_comtrade_payload(
            reporter_code=reporter_code,
            cmd_code=cmd_code,
            period=period,
            flow_code='M,X',
            api_key=api_key,
        )
        if _comtrade_payload_has_export_rows(payload):
            return True
    return False


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'{path.name}.tmp')
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp_path.replace(path)


def _comtrade_supportability_base_artifact(
    *,
    requested_pair_count: int,
    period_chunks: list[str],
    rows: list[dict[str, Any]],
    reused_pair_count: int = 0,
    blocked_error: str | None = None,
) -> dict[str, Any]:
    completed_rows = [row for row in rows if row.get('status') == 'completed']
    available_rows = [row for row in completed_rows if row.get('available') is True]
    unavailable_rows = [row for row in completed_rows if row.get('available') is False]
    blocked_rows = [row for row in rows if row.get('status') == 'blocked']
    unprobed_pair_count = max(0, requested_pair_count - len(completed_rows) - len(blocked_rows))
    if blocked_rows:
        status = 'blocked'
    elif len(completed_rows) < requested_pair_count:
        status = 'partial'
    else:
        status = 'complete'
    return {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'provider': 'Comtrade',
        'artifact_version': COMTRADE_SUPPORTABILITY_ARTIFACT_VERSION,
        'endpoint': COMTRADE_SUPPORTABILITY_ENDPOINT,
        'probe_scope': 'selection_prefix',
        'probe_contract': COMTRADE_SUPPORTABILITY_PROBE_CONTRACT,
        'partner_code': COMTRADE_SUPPORTABILITY_PARTNER_CODE,
        'freq_code': COMTRADE_SUPPORTABILITY_FREQ_CODE,
        'flow_codes': list(COMTRADE_SUPPORTABILITY_FLOW_CODES),
        'requested_pair_count': requested_pair_count,
        'completed_pair_count': len(completed_rows),
        'available_pair_count': len(available_rows),
        'unavailable_pair_count': len(unavailable_rows),
        'blocked_pair_count': len(blocked_rows),
        'unprobed_pair_count': unprobed_pair_count,
        'reused_pair_count': reused_pair_count,
        'status': status,
        'blocked_error': blocked_error,
        'reason': COMTRADE_SUPPORTABILITY_REASON,
        'period_chunks': period_chunks,
        'rows': rows,
    }


def _comtrade_completed_cache_from_artifact(
    *,
    artifact_path: Path | None,
    period_chunks: list[str],
) -> dict[str, dict[str, Any]]:
    if artifact_path is None or not artifact_path.exists():
        return {}
    try:
        artifact = load_json(artifact_path)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(artifact, dict):
        return {}
    expected_header = {
        'artifact_version': COMTRADE_SUPPORTABILITY_ARTIFACT_VERSION,
        'endpoint': COMTRADE_SUPPORTABILITY_ENDPOINT,
        'probe_contract': COMTRADE_SUPPORTABILITY_PROBE_CONTRACT,
        'partner_code': COMTRADE_SUPPORTABILITY_PARTNER_CODE,
        'freq_code': COMTRADE_SUPPORTABILITY_FREQ_CODE,
        'flow_codes': list(COMTRADE_SUPPORTABILITY_FLOW_CODES),
        'period_chunks': period_chunks,
    }
    for field, expected in expected_header.items():
        if artifact.get(field) != expected:
            return {}
    cached: dict[str, dict[str, Any]] = {}
    rows = artifact.get('rows')
    if not isinstance(rows, list):
        return cached
    for row in rows:
        if not isinstance(row, dict) or row.get('status') != 'completed':
            continue
        key = str(row.get('key') or '').strip()
        reporter_code, _, cmd_code = key.partition(':')
        if (
            not reporter_code
            or not cmd_code
            or str(row.get('reporter_code') or '') != reporter_code
            or str(row.get('source_indicator_code') or '') != cmd_code
            or row.get('endpoint') != COMTRADE_SUPPORTABILITY_ENDPOINT
            or row.get('probe_contract') != COMTRADE_SUPPORTABILITY_PROBE_CONTRACT
            or row.get('partner_code') != COMTRADE_SUPPORTABILITY_PARTNER_CODE
            or row.get('freq_code') != COMTRADE_SUPPORTABILITY_FREQ_CODE
            or row.get('flow_codes') != list(COMTRADE_SUPPORTABILITY_FLOW_CODES)
            or row.get('period_chunks') != period_chunks
            or row.get('available') not in {True, False}
        ):
            continue
        cached[key] = row
    return cached


def _comtrade_artifact_row(
    *,
    key: str,
    reporter_code: str,
    cmd_code: str,
    row: dict[str, Any],
    period_chunks: list[str],
    status: str,
    available: bool | None,
    reused_from_artifact: bool = False,
    error_class: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    return {
        'key': key,
        'provider': 'Comtrade',
        'endpoint': COMTRADE_SUPPORTABILITY_ENDPOINT,
        'probe_contract': COMTRADE_SUPPORTABILITY_PROBE_CONTRACT,
        'partner_code': COMTRADE_SUPPORTABILITY_PARTNER_CODE,
        'freq_code': COMTRADE_SUPPORTABILITY_FREQ_CODE,
        'flow_codes': list(COMTRADE_SUPPORTABILITY_FLOW_CODES),
        'period_chunks': period_chunks,
        'reporter_code': reporter_code,
        'source_indicator_code': cmd_code,
        'name': row.get('name'),
        'status': status,
        'available': available,
        'supportability_reason': (
            COMTRADE_SUPPORTABILITY_REASON
            if status == 'completed' and available is False
            else None
        ),
        'reused_from_artifact': reused_from_artifact,
        'error_class': error_class,
        'error_message': error_message,
    }


def comtrade_export_unavailability_by_key(
    rows: list[dict[str, Any]],
    *,
    artifact_path: Path | None = None,
    max_rows: int | None = None,
    api_key: str | None = None,
) -> dict[str, str]:
    """Return exact Comtrade reporter/HS export surfaces with no observations.

    The materializer synthesizes user-answerability Comtrade prompts by pairing
    a sampled HS code with a default reporter country.  A sparse
    reporter/commodity pair that has no public UN Comtrade observations is not
    claim evidence for runtime failure; it must be inventoried and replaced
    before scoring.  The probe is mechanical: exact reporter code, HS code,
    world partner, annual frequency, and export flow only.
    """
    limited_rows = rows[:max(0, max_rows)] if max_rows is not None else rows
    key_to_row: dict[str, dict[str, Any]] = {}
    for row in limited_rows:
        key = _comtrade_probe_key(row)
        if key:
            key_to_row.setdefault(key, row)
    if not key_to_row:
        return {}
    resolved_api_key = str(api_key if api_key is not None else _load_env_value('COMTRADE_API_KEY')).strip()
    period_chunks = _comtrade_period_chunks()
    cached_completed = _comtrade_completed_cache_from_artifact(
        artifact_path=artifact_path,
        period_chunks=period_chunks,
    )
    unavailable: dict[str, str] = {}
    artifact_rows: list[dict[str, Any]] = []
    reused_pair_count = 0
    for key, row in key_to_row.items():
        reporter_code, cmd_code = key.split(':', 1)
        cached_row = cached_completed.get(key)
        if cached_row is not None:
            available = bool(cached_row.get('available'))
            if not available:
                unavailable[key] = COMTRADE_SUPPORTABILITY_REASON
            artifact_rows.append(
                _comtrade_artifact_row(
                    key=key,
                    reporter_code=reporter_code,
                    cmd_code=cmd_code,
                    row=row,
                    period_chunks=period_chunks,
                    status='completed',
                    available=available,
                    reused_from_artifact=True,
                )
            )
            reused_pair_count += 1
            continue
        try:
            available = _comtrade_export_observations_available(
                reporter_code=reporter_code,
                cmd_code=cmd_code,
                api_key=resolved_api_key,
                period_chunks=period_chunks,
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            artifact_rows.append(
                _comtrade_artifact_row(
                    key=key,
                    reporter_code=reporter_code,
                    cmd_code=cmd_code,
                    row=row,
                    period_chunks=period_chunks,
                    status='blocked',
                    available=None,
                    error_class=exc.__class__.__name__,
                    error_message=str(exc),
                )
            )
            if artifact_path is not None:
                _write_json_atomic(
                    artifact_path,
                    _comtrade_supportability_base_artifact(
                        requested_pair_count=len(key_to_row),
                        period_chunks=period_chunks,
                        rows=artifact_rows,
                        reused_pair_count=reused_pair_count,
                        blocked_error=str(exc),
                    ),
                )
            raise RuntimeError(
                'Comtrade export supportability probe failed; '
                'rerun when the provider contract is reachable or disable the probe explicitly'
            ) from exc
        if not available:
            unavailable[key] = COMTRADE_SUPPORTABILITY_REASON
        artifact_rows.append(
            _comtrade_artifact_row(
                key=key,
                reporter_code=reporter_code,
                cmd_code=cmd_code,
                row=row,
                period_chunks=period_chunks,
                status='completed',
                available=available,
            )
        )
        if artifact_path is not None:
            _write_json_atomic(
                artifact_path,
                _comtrade_supportability_base_artifact(
                    requested_pair_count=len(key_to_row),
                    period_chunks=period_chunks,
                    rows=artifact_rows,
                    reused_pair_count=reused_pair_count,
                ),
            )
    if artifact_path is not None:
        _write_json_atomic(
            artifact_path,
            _comtrade_supportability_base_artifact(
                requested_pair_count=len(key_to_row),
                period_chunks=period_chunks,
                rows=artifact_rows,
                reused_pair_count=reused_pair_count,
            ),
        )
    return unavailable


def comtrade_supportability_probe_keys(
    rows: list[dict[str, Any]],
    *,
    max_rows: int | None = None,
) -> set[str]:
    limited_rows = rows[:max(0, max_rows)] if max_rows is not None else rows
    return {
        key
        for row in limited_rows
        for key in [_comtrade_probe_key(row)]
        if key
    }


def direct_record_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    provenance = dict(record.get('provenance') or {})
    origin = dict(record.get('origin') or {})
    risk_level = str(provenance.get('query_quality_risk') or 'low')
    risk_rank = {'low': 0, 'medium': 1, 'high': 2}.get(risk_level, 3)
    reasons = list(provenance.get('query_quality_reasons') or [])
    selection_reasons = list(provenance.get('selection_quality_reasons') or reasons)
    inventory_only_reason_count = len(selection_reasons)
    selection_supportability_reason = str(provenance.get('selection_supportability_reason') or '').strip()
    provider_anchor = bool(provenance.get('user_answerability_sampling_anchor'))
    popularity = float(origin.get('popularity') or 0.0)
    return (
        1 if selection_supportability_reason else 0,
        risk_rank,
        0 if provider_anchor else 1,
        inventory_only_reason_count,
        0 if popularity > 0 else 1,
        -int(popularity),
        len(str(record.get('query') or '')),
        len(reasons),
        str(record.get('id') or ''),
    )


def select_balanced_quality_records(records: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    selectable = [
        record
        for record in records
        if not str((record.get('provenance') or {}).get('selection_supportability_reason') or '').strip()
    ]
    return sorted(selectable, key=direct_record_sort_key)[:count]


def build_direct_rows(
    *,
    provider: str,
    count: int,
    snapshot_meta: dict[str, Any],
    db_path: Path,
    seed: int,
    dataset_tier: str,
    holdout_split: str,
    session_id_prefix: str,
    allow_provider_cap_truncation: bool,
    supportability_artifact_dir: Path | None = None,
    probe_comtrade_supportability: bool = False,
    max_comtrade_probe_rows: int | None = None,
) -> tuple[list[dict[str, Any]], int, int, list[dict[str, Any]]]:
    provider_population = provider_catalog_count(provider, db_path)
    rows = sample_indicator_rows(provider, provider_population, db_path=db_path.resolve(), seed=seed)
    coingecko_price_unavailable: dict[str, str] = {}
    comtrade_unavailable: dict[str, str] = {}
    comtrade_probed_keys: set[str] = set()
    if provider.upper() == 'COINGECKO':
        artifact_path = (
            supportability_artifact_dir / 'coingecko_price_supportability_probe.json'
            if supportability_artifact_dir is not None
            else None
        )
        coingecko_price_unavailable = coingecko_price_unavailability_by_code(
            rows,
            artifact_path=artifact_path,
            max_slugs=max(count * 2, count + COINGECKO_SIMPLE_PRICE_BATCH_SIZE),
        )
    if provider.upper() == 'COMTRADE' and probe_comtrade_supportability:
        comtrade_probe_limit = (
            max_comtrade_probe_rows
            if max_comtrade_probe_rows is not None
            else max(count * 2, count + 200)
        )
        comtrade_probed_keys = comtrade_supportability_probe_keys(
            rows,
            max_rows=comtrade_probe_limit,
        )
        artifact_path = (
            supportability_artifact_dir / 'comtrade_export_supportability_probe.json'
            if supportability_artifact_dir is not None
            else None
        )
        comtrade_unavailable = comtrade_export_unavailability_by_key(
            rows,
            artifact_path=artifact_path,
            max_rows=comtrade_probe_limit,
        )
    if count > len(rows):
        if not allow_provider_cap_truncation:
            raise RuntimeError(f'requested {count} {provider} rows, but only {len(rows)} are selectable')
        count = len(rows)
    records: list[dict[str, Any]] = []
    supportability_inventory: list[dict[str, Any]] = []
    supportability_excluded_count = 0
    quality_excluded_count = 0
    for seq, row in enumerate(rows, start=1):
        supportability_probe = {
            'provider_stratum': provider,
            'origin': {
                'source_provider': provider,
                'source_indicator_code': row.get('code'),
                'name': row.get('name'),
                'category': row.get('category'),
                'raw_metadata': row.get('raw_metadata'),
            },
            'evaluation_target': CERTIFICATION_TARGET_USER_ANSWERABILITY,
        }
        supportability_reason = selection_supportability_reason_for_row(supportability_probe)
        if not supportability_reason:
            slug = _coingecko_slug_for_price_probe(row)
            supportability_reason = coingecko_price_unavailable.get(slug)
        if not supportability_reason and provider.upper() == 'COMTRADE' and probe_comtrade_supportability:
            comtrade_key = _comtrade_probe_key(row)
            if comtrade_key and comtrade_key not in comtrade_probed_keys:
                raise RuntimeError(
                    f'Comtrade supportability probe scope exhausted before filling {count} rows; '
                    f'increase --max-comtrade-probe-rows or inspect supportability inventory'
                )
            supportability_reason = comtrade_unavailable.get(comtrade_key)
        if supportability_reason:
            supportability_excluded_count += 1
            supportability_inventory.append(
                {
                    'candidate_id': prefixed_session_id(f"balanced-direct-{provider.lower()}-{seq:06d}", session_id_prefix),
                    'provider': provider,
                    'source_indicator_code': row.get('code'),
                    'name': row.get('name'),
                    'category': row.get('category'),
                    'supportability_reason': supportability_reason,
                    'disposition': 'excluded_before_selection',
                    'replacement_basis': 'same_provider_fill_continued_until_target_or_cap',
                }
            )
            continue
        record = build_direct_record(
            row,
            seq,
            provider_count=count,
            provider_sample_count=count,
            snapshot_id=snapshot_id(snapshot_meta),
            seed=seed,
            holdout_split=holdout_split,
            dataset_tier=dataset_tier,
            certification_target=CERTIFICATION_TARGET_USER_ANSWERABILITY,
        )
        record['id'] = prefixed_session_id(f"balanced-direct-{provider.lower()}-{seq:06d}", session_id_prefix)
        provenance = record.setdefault('provenance', {})
        provenance['batch_plan'] = 'balanced_claim_holdout'
        provenance['sampling_probability'] = 1.0
        provenance['selection_weight'] = 1.0
        provenance['balanced_claim_holdout_provider_target_n'] = count
        provenance['balanced_claim_holdout_provider_catalog_n'] = provider_population
        quality = audit_direct_query_shape(record)
        provenance['query_quality_risk'] = quality['risk_level']
        provenance['query_quality_reasons'] = quality['reasons']
        provenance['selection_quality_reasons'] = quality['reasons']
        if quality['risk_level'] == 'high':
            quality_excluded_count += 1
            continue
        records.append(record)
        if len(records) >= count:
            break
    if len(records) < count:
        if not allow_provider_cap_truncation:
            raise RuntimeError(
                f'balanced claim holdout could not fill {provider} target {count} after high-risk query exclusion'
            )
        count = len(records)
        for record in records:
            provenance = record.setdefault('provenance', {})
            provenance['balanced_claim_holdout_provider_target_n'] = count
    for record in records:
        provenance = record.setdefault('provenance', {})
        provenance['balanced_claim_holdout_high_risk_excluded_before_fill'] = quality_excluded_count
        provenance['balanced_claim_holdout_supportability_excluded_before_fill'] = supportability_excluded_count
    return records, supportability_excluded_count, quality_excluded_count, supportability_inventory


def build_multiround_rows(
    *,
    family: str,
    count: int,
    snapshot_meta: dict[str, Any],
    seed: int,
    dataset_tier: str,
    holdout_split: str,
    session_id_prefix: str,
) -> list[dict[str, Any]]:
    rows = []
    builder = MULTI_BUILDERS[family]
    for seq in range(1, count + 1):
        session = builder(seq)
        session['id'] = prefixed_session_id(f'balanced-{family}-{seq:06d}', session_id_prefix)
        record = apply_certification_target(
            annotate_multiround(
                session,
                snapshot_id=snapshot_id(snapshot_meta),
                seed=seed,
                holdout_split=holdout_split,
                dataset_tier=dataset_tier,
                family_total_count=count,
                family_sample_count=count,
            ),
            CERTIFICATION_TARGET_USER_ANSWERABILITY,
        )
        provenance = record.setdefault('provenance', {})
        provenance['batch_plan'] = 'balanced_claim_holdout'
        provenance['balanced_claim_holdout_family_target_n'] = count
        rows.append(record)
    return rows


def build_ambiguity_rows(
    *,
    family: str,
    count: int,
    snapshot_meta: dict[str, Any],
    seed: int,
    dataset_tier: str,
    holdout_split: str,
    session_id_prefix: str,
) -> list[dict[str, Any]]:
    rows = []
    templates = FAMILY_TEMPLATES[family]
    for idx in range(count):
        query, behavior, outcomes = templates[idx % len(templates)]
        record = apply_certification_target(
            make_ambiguity_record(
                family,
                idx + 1,
                query,
                behavior,
                outcomes,
                snapshot_id=snapshot_id(snapshot_meta),
                seed=seed,
                holdout_split=holdout_split,
                dataset_tier=dataset_tier,
                family_total_count=count,
                family_sample_count=count,
            ),
            CERTIFICATION_TARGET_USER_ANSWERABILITY,
        )
        record['id'] = prefixed_session_id(str(record.get('id') or ''), session_id_prefix)
        provenance = record.setdefault('provenance', {})
        provenance['batch_plan'] = 'balanced_claim_holdout'
        provenance['balanced_claim_holdout_family_target_n'] = count
        rows.append(record)
    return rows


def comtrade_probe_manifest_metadata(comtrade_probe_path: Path) -> dict[str, Any] | None:
    if not comtrade_probe_path.exists():
        return None
    comtrade_probe_payload = load_json(comtrade_probe_path)
    return {
        'artifact_path': str(comtrade_probe_path),
        'artifact_sha256': file_sha256(comtrade_probe_path),
        'endpoint': comtrade_probe_payload.get('endpoint'),
        'probe_scope': comtrade_probe_payload.get('probe_scope'),
        'requested_pair_count': comtrade_probe_payload.get('requested_pair_count'),
        'completed_pair_count': comtrade_probe_payload.get('completed_pair_count'),
        'available_pair_count': comtrade_probe_payload.get('available_pair_count'),
        'unavailable_pair_count': comtrade_probe_payload.get('unavailable_pair_count'),
        'blocked_pair_count': comtrade_probe_payload.get('blocked_pair_count'),
        'unprobed_pair_count': comtrade_probe_payload.get('unprobed_pair_count'),
        'reused_pair_count': comtrade_probe_payload.get('reused_pair_count'),
        'artifact_version': comtrade_probe_payload.get('artifact_version'),
        'status': comtrade_probe_payload.get('status'),
        'reason': comtrade_probe_payload.get('reason'),
        'generated_at_utc': comtrade_probe_payload.get('generated_at_utc'),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Materialize a coherent balanced claim holdout with unit weights inside each required stratum.')
    parser.add_argument('--floor-policy', type=Path, default=DEFAULT_FLOOR_POLICY)
    parser.add_argument('--snapshot', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('--db-path', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--direct-per-provider', type=int, default=381)
    parser.add_argument('--family-per-stratum', type=int, default=381)
    parser.add_argument('--seed', type=int, default=20260516)
    parser.add_argument('--dataset-tier', default='cert_holdout')
    parser.add_argument('--holdout-split', default='balanced_claim_holdout_v1')
    parser.add_argument('--session-id-prefix', default='balanced-claim-v1')
    parser.add_argument(
        '--allow-provider-cap-truncation',
        action='store_true',
        help='Use all finite/supportability-selectable rows for providers with fewer rows than the requested target, with manifest disclosure.',
    )
    parser.add_argument(
        '--probe-comtrade-supportability',
        action='store_true',
        help='Probe exact UN Comtrade reporter/HS export availability before selecting Comtrade direct rows.',
    )
    parser.add_argument(
        '--max-comtrade-probe-rows',
        type=int,
        default=None,
        help='Maximum sampled Comtrade rows to probe for availability; defaults to a bounded fill buffer.',
    )
    args = parser.parse_args()

    if args.direct_per_provider <= 0 or args.family_per_stratum <= 0:
        raise ValueError('per-stratum targets must be positive')

    floor_policy = load_json(args.floor_policy.resolve())
    snapshot_meta = load_json(args.snapshot.resolve())
    direct_policy = required_groups(floor_policy, 'required_direct_provider_floors')
    multiround_policy = required_groups(floor_policy, 'required_multiround_family_floors')
    ambiguity_policy = required_groups(floor_policy, 'required_ambiguity_family_floors')

    supportability_prefiltered: dict[str, int] = {}
    quality_excluded_before_fill: dict[str, int] = {}
    supportability_inventory_rows: list[dict[str, Any]] = []
    direct_rows: list[dict[str, Any]] = []
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for provider in sorted(direct_policy):
        rows, prefiltered, quality_excluded, provider_supportability_inventory = build_direct_rows(
            provider=provider,
            count=args.direct_per_provider,
            snapshot_meta=snapshot_meta,
            db_path=args.db_path,
            seed=args.seed,
            dataset_tier=args.dataset_tier,
            holdout_split=args.holdout_split,
            session_id_prefix=args.session_id_prefix,
            allow_provider_cap_truncation=args.allow_provider_cap_truncation,
            supportability_artifact_dir=output_dir,
            probe_comtrade_supportability=args.probe_comtrade_supportability,
            max_comtrade_probe_rows=args.max_comtrade_probe_rows,
        )
        direct_rows.extend(rows)
        supportability_prefiltered[provider] = prefiltered
        quality_excluded_before_fill[provider] = quality_excluded
        supportability_inventory_rows.extend(provider_supportability_inventory)

    multiround_rows: list[dict[str, Any]] = []
    for family in sorted(multiround_policy):
        multiround_rows.extend(
            build_multiround_rows(
                family=family,
                count=args.family_per_stratum,
                snapshot_meta=snapshot_meta,
                seed=args.seed,
                dataset_tier=args.dataset_tier,
                holdout_split=args.holdout_split,
                session_id_prefix=args.session_id_prefix,
            )
        )

    ambiguity_rows: list[dict[str, Any]] = []
    for family in sorted(ambiguity_policy):
        ambiguity_rows.extend(
            build_ambiguity_rows(
                family=family,
                count=args.family_per_stratum,
                snapshot_meta=snapshot_meta,
                seed=args.seed,
                dataset_tier=args.dataset_tier,
                holdout_split=args.holdout_split,
                session_id_prefix=args.session_id_prefix,
            )
        )

    write_jsonl(output_dir / 'balanced_direct.jsonl', direct_rows)
    write_jsonl(output_dir / 'balanced_multiround.jsonl', multiround_rows)
    write_jsonl(output_dir / 'balanced_ambiguity.jsonl', ambiguity_rows)
    write_jsonl(output_dir / 'balanced_all.jsonl', direct_rows + multiround_rows + ambiguity_rows)
    supportability_inventory_path = output_dir / 'supportability_inventory.jsonl'
    write_jsonl(supportability_inventory_path, supportability_inventory_rows)
    coingecko_probe_path = output_dir / 'coingecko_price_supportability_probe.json'
    coingecko_probe_metadata: dict[str, Any] | None = None
    if coingecko_probe_path.exists():
        coingecko_probe_payload = load_json(coingecko_probe_path)
        coingecko_probe_metadata = {
            'artifact_path': str(coingecko_probe_path),
            'artifact_sha256': file_sha256(coingecko_probe_path),
            'endpoint': coingecko_probe_payload.get('endpoint'),
            'metric': coingecko_probe_payload.get('metric'),
            'requested_slug_count': coingecko_probe_payload.get('requested_slug_count'),
            'probe_scope': coingecko_probe_payload.get('probe_scope'),
            'unavailable_slug_count': coingecko_probe_payload.get('unavailable_slug_count'),
            'reason': coingecko_probe_payload.get('reason'),
            'generated_at_utc': coingecko_probe_payload.get('generated_at_utc'),
        }
    comtrade_probe_path = output_dir / 'comtrade_export_supportability_probe.json'
    comtrade_probe_metadata = comtrade_probe_manifest_metadata(comtrade_probe_path)
    manifest = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'floor_policy_path': str(args.floor_policy.resolve()),
        'snapshot_path': str(args.snapshot.resolve()),
        'certification_target': CERTIFICATION_TARGET_USER_ANSWERABILITY,
        'method': 'balanced_required_stratum_unit_weight_holdout',
        'direct_per_provider': args.direct_per_provider,
        'family_per_stratum': args.family_per_stratum,
        'direct_records': len(direct_rows),
        'multiround_records': len(multiround_rows),
        'ambiguity_records': len(ambiguity_rows),
        'total_records': len(direct_rows) + len(multiround_rows) + len(ambiguity_rows),
        'supportability_prefiltered_counts': supportability_prefiltered,
        'supportability_inventory_path': str(supportability_inventory_path),
        'supportability_inventory_records': len(supportability_inventory_rows),
        'supportability_prefiltered_reason_counts': {
            reason: sum(
                1
                for row in supportability_inventory_rows
                if row.get('supportability_reason') == reason
            )
            for reason in sorted({str(row.get('supportability_reason') or '') for row in supportability_inventory_rows})
            if reason
        },
        'quality_screening': {
            'audit': 'audit_direct_query_shape',
            'excluded_risk_levels': ['high'],
            'disposition': 'excluded_before_selection_not_runtime_support_evidence',
        },
        'provider_contract_supportability': {
            'CoinGecko': {
                'probe': 'simple/price',
                'metric': 'usd',
                'reason': 'coingecko_current_price_unavailable',
                'disposition': 'excluded_before_selection_provider_contract_inventory_not_runtime_support_evidence',
                **(coingecko_probe_metadata or {}),
            },
            'Comtrade': {
                'probe': 'reporter/HS export observations',
                'reason': COMTRADE_SUPPORTABILITY_REASON,
                'enabled': bool(args.probe_comtrade_supportability),
                'disposition': 'excluded_before_selection_provider_contract_inventory_not_runtime_support_evidence',
                **(comtrade_probe_metadata or {}),
            },
        },
        'quality_excluded_counts_before_fill': quality_excluded_before_fill,
        'include_supportability_probes': False,
        'allow_provider_cap_truncation': bool(args.allow_provider_cap_truncation),
        'actual_direct_counts_by_provider': {
            provider: sum(1 for row in direct_rows if row.get('provider_stratum') == provider)
            for provider in sorted(direct_policy)
        },
        'actual_multiround_counts_by_family': {
            family: sum(1 for row in multiround_rows if row.get('family') == family)
            for family in sorted(multiround_policy)
        },
        'actual_ambiguity_counts_by_family': {
            family: sum(
                1
                for row in ambiguity_rows
                if str(((row.get('provenance') or {}).get('family')) or '') == family
            )
            for family in sorted(ambiguity_policy)
        },
    }
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output_dir': str(output_dir), **manifest}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
