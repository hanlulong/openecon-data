#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT = ROOT / 'validation' / 'manifests' / 'catalog_snapshot-2026-04-14.json'
DEFAULT_PROVIDER_DISTRIBUTION = ROOT / 'validation' / 'manifests' / 'provider_distribution-latest.json'
DEFAULT_STRATA = ROOT / 'validation' / 'manifests' / 'strata_definition-v2.json'
DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'claim_evidence_package.json'
DEFAULT_DEPLOYMENT_BASE_URL = 'https://data.openecon.ai'


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=ROOT,
            text=True,
        ).strip()
    except Exception:
        return None


def git_dirty() -> bool | None:
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())
    except Exception:
        return None


def artifact_entry(path: Path | None, *, summary: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.resolve()
    if not resolved.exists():
        return {
            'path': str(resolved),
            'exists': False,
            'sha256': None,
            'summary': summary,
        }
    return {
        'path': str(resolved),
        'exists': True,
        'sha256': sha256_file(resolved),
        'summary': summary,
    }


def health_summary(base_url: str | None) -> dict[str, Any] | None:
    if not base_url:
        return None
    url = base_url.rstrip('/') + '/api/health'
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
        return {
            'base_url': base_url,
            'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
            'status': payload.get('status'),
            'service_timestamp': payload.get('timestamp'),
            'environment': payload.get('environment'),
        }
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {
            'base_url': base_url,
            'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
            'error': str(exc),
        }


def resolve_deployment_timestamp(explicit_timestamp: str | None, deployment_health: dict[str, Any] | None) -> str | None:
    if explicit_timestamp:
        return explicit_timestamp
    if isinstance(deployment_health, dict):
        service_timestamp = deployment_health.get('service_timestamp')
        if isinstance(service_timestamp, str) and service_timestamp.strip():
            return service_timestamp
    return None


def score_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    metrics = dict(report.get('metrics') or {})
    snapshot = dict(report.get('snapshot') or {})
    return {
        'claim_grade_ready': report.get('claim_grade_ready'),
        'scoring_mode': report.get('scoring_mode'),
        'snapshot_id': report.get('snapshot_id'),
        'session_count': snapshot.get('session_count'),
        'claim_observed_success': metrics.get('claim_observed_success'),
        'claim_lower95': metrics.get('claim_lower95'),
        'claim_grade_blockers': list(report.get('claim_grade_blockers') or []),
    }


def claim_decision_summary(decision: dict[str, Any] | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        'claim_allowed': decision.get('claim_allowed'),
        'observed_success': decision.get('observed_success'),
        'lower95': decision.get('lower95'),
        'blockers': list(decision.get('blockers') or []),
        'framework_bug_cluster_counts': dict(decision.get('framework_bug_cluster_counts') or {}),
        'parity_material_drift_sessions': decision.get('parity_material_drift_sessions'),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Build a hashed evidence package tying claim artifacts, git SHA, and deployment metadata together.')
    parser.add_argument('--catalog-snapshot', type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument('--provider-distribution', type=Path, default=DEFAULT_PROVIDER_DISTRIBUTION)
    parser.add_argument('--strata-definition', type=Path, default=DEFAULT_STRATA)
    parser.add_argument('--local-score-report', type=Path, required=True)
    parser.add_argument('--adjudication-summary', type=Path, required=True)
    parser.add_argument('--triage-report', type=Path, default=None)
    parser.add_argument('--production-score-report', type=Path, required=True)
    parser.add_argument('--claim-decision', type=Path, required=True)
    parser.add_argument('--parity-report', type=Path, default=None)
    parser.add_argument('--deployment-base-url', default=DEFAULT_DEPLOYMENT_BASE_URL)
    parser.add_argument('--deployment-timestamp', default=None, help='Optional explicit deployment timestamp (UTC ISO-8601)')
    parser.add_argument('--git-sha', default=None, help='Optional exact git SHA override')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    snapshot = load_json(args.catalog_snapshot.resolve())
    provider_distribution = load_json(args.provider_distribution.resolve())
    strata_definition = load_json(args.strata_definition.resolve())
    local_score = load_json(args.local_score_report.resolve())
    adjudication_summary = load_json(args.adjudication_summary.resolve())
    triage_report = load_json(args.triage_report.resolve()) if args.triage_report is not None else None
    production_score = load_json(args.production_score_report.resolve())
    claim_decision = load_json(args.claim_decision.resolve())
    parity_report = load_json(args.parity_report.resolve()) if args.parity_report is not None else None

    resolved_git_sha = args.git_sha or git_sha()
    deployment_health = health_summary(args.deployment_base_url)
    deployment_timestamp = resolve_deployment_timestamp(args.deployment_timestamp, deployment_health)
    package_blockers: list[str] = []
    if claim_decision is not None:
        package_blockers.extend(list(claim_decision.get('blockers') or []))
    if not resolved_git_sha:
        package_blockers.append('exact git SHA unavailable')
    if deployment_timestamp is None:
        package_blockers.append('deployment timestamp missing')

    package = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'package_version': 1,
        'claim_context': {
            'snapshot_id': (local_score or {}).get('snapshot_id') or (provider_distribution or {}).get('snapshot_id'),
            'indicator_count': (snapshot or {}).get('indicator_count'),
            'git_sha': resolved_git_sha,
            'git_dirty': git_dirty(),
            'deployment_base_url': args.deployment_base_url,
            'deployment_timestamp_utc': deployment_timestamp,
            'deployment_health': deployment_health,
        },
        'artifacts': {
            'catalog_snapshot_manifest': artifact_entry(args.catalog_snapshot, summary={
                'snapshot_date': (snapshot or {}).get('snapshot_date'),
                'indicator_count': (snapshot or {}).get('indicator_count'),
                'snapshot_git_sha': (snapshot or {}).get('git_sha'),
            }),
            'provider_distribution_summary': artifact_entry(args.provider_distribution, summary={
                'snapshot_id': (provider_distribution or {}).get('snapshot_id'),
                'indicator_count': (provider_distribution or {}).get('indicator_count'),
            }),
            'strata_definition': artifact_entry(args.strata_definition, summary={
                'snapshot_id': (strata_definition or {}).get('snapshot_id'),
                'version': (strata_definition or {}).get('version'),
            }),
            'local_score_report': artifact_entry(args.local_score_report, summary=score_summary(local_score)),
            'adjudication_summary': artifact_entry(args.adjudication_summary, summary={
                'adjudication_complete': (adjudication_summary or {}).get('adjudication_complete'),
                'completed_records': (adjudication_summary or {}).get('completed_records'),
                'total_records': (adjudication_summary or {}).get('total_records'),
            }),
            'triage_report': artifact_entry(args.triage_report, summary={
                'bucket_counts': ((triage_report or {}).get('summary') or {}).get('bucket_counts'),
                'framework_bug_cluster_counts': ((triage_report or {}).get('summary') or {}).get('framework_bug_cluster_counts'),
            }) if args.triage_report is not None else None,
            'production_score_report': artifact_entry(args.production_score_report, summary=score_summary(production_score)),
            'parity_report': artifact_entry(args.parity_report, summary={
                'sessions_with_differences': ((parity_report or {}).get('summary') or {}).get('sessions_with_differences'),
                'severity_counts': ((parity_report or {}).get('summary') or {}).get('severity_counts'),
            }) if args.parity_report is not None else None,
            'claim_decision': artifact_entry(args.claim_decision, summary=claim_decision_summary(claim_decision)),
        },
        'summary': {
            'claim_allowed': (claim_decision or {}).get('claim_allowed'),
            'claim_blockers': list((claim_decision or {}).get('blockers') or []),
            'package_blockers': package_blockers,
            'local_score': score_summary(local_score),
            'production_score': score_summary(production_score),
        },
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(package, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'claim_allowed': package['summary']['claim_allowed']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
