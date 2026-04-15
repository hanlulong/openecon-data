#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import time
import sys
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.utils.providers import normalize_provider_name  # noqa: E402
from scripts.validation.run_certification import extract_response_signals, iter_jsonl  # noqa: E402

DEFAULT_OUTPUT = ROOT / 'validation_private' / 'reports' / 'direct_batch_viability.json'


def accepted_provider_set(row: dict[str, Any]) -> set[str]:
    gold = dict(row.get('gold') or {})
    accepted = gold.get('accepted_providers') or []
    return {normalize_provider_name(str(item)) for item in accepted if str(item).strip()}


def probe_query(
    *,
    base: str,
    query: str,
    timeout_seconds: float,
    max_attempts: int,
) -> tuple[requests.Response | None, dict[str, Any] | None, str | None, int]:
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(base, json={'query': query}, timeout=timeout_seconds)
            data = resp.json()
            return resp, data, None, attempt
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= max_attempts:
                return None, None, last_error, attempt
            time.sleep(min(1.0, 0.1 * attempt))
        except Exception as exc:  # pragma: no cover - defensive
            last_error = f"{type(exc).__name__}: {exc}"
            return None, None, last_error, attempt
    return None, None, last_error or "unknown", max_attempts


def main() -> int:
    parser = argparse.ArgumentParser(description='Probe direct-batch viability against a live backend before full execution.')
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--kept-output', type=Path, default=None)
    parser.add_argument('--rejected-output', type=Path, default=None)
    parser.add_argument('--max-sessions', type=int, default=None)
    parser.add_argument('--timeout-seconds', type=float, default=20.0)
    parser.add_argument('--max-attempts', type=int, default=2)
    args = parser.parse_args()

    rows = list(iter_jsonl(args.dataset.resolve()))
    if args.max_sessions is not None:
        rows = rows[:args.max_sessions]

    results = []
    reason_counts = Counter()
    kept_rows = []
    rejected_rows = []
    base = args.base_url.rstrip('/') + '/api/query'

    for row in rows:
        accepted = accepted_provider_set(row)
        resp, data, request_error, attempt_count = probe_query(
            base=base,
            query=str(row.get('query') or ''),
            timeout_seconds=float(args.timeout_seconds),
            max_attempts=max(1, int(args.max_attempts)),
        )

        if resp is None or data is None:
            result = {
                'session_id': row.get('id'),
                'query': row.get('query'),
                'provider_stratum': row.get('provider_stratum'),
                'status_code': None,
                'viability_pass': False,
                'structural_pass': False,
                'provider_match': False,
                'accepted_providers': sorted(accepted),
                'observed_providers': [],
                'series_count': 0,
                'clarification_detected': False,
                'error': request_error,
                'reasons': [f"exception:{request_error.split(':', 1)[0]}" if request_error else 'exception:unknown', 'no_series'],
                'attempt_count': attempt_count,
            }
        else:
            signals = extract_response_signals(data)
            observed_providers = {normalize_provider_name(provider) for provider in signals['providers']}
            structural_pass = int(resp.status_code or 0) == 200 and not data.get('error') and int(signals['series_count'] or 0) > 0 and not signals['clarification_detected']
            provider_match = True if not accepted else bool(accepted & observed_providers)

            reasons = []
            if not structural_pass:
                if data.get('error'):
                    reasons.append(f"error:{data.get('error')}")
                if signals['clarification_detected']:
                    reasons.append('clarification_detected')
                if int(signals['series_count'] or 0) <= 0:
                    reasons.append('no_series')
            if structural_pass and not provider_match:
                reasons.append('provider_mismatch')

            viability_pass = structural_pass and provider_match
            result = {
                'session_id': row.get('id'),
                'query': row.get('query'),
                'provider_stratum': row.get('provider_stratum'),
                'status_code': resp.status_code,
                'viability_pass': viability_pass,
                'structural_pass': structural_pass,
                'provider_match': provider_match,
                'accepted_providers': sorted(accepted),
                'observed_providers': sorted(observed_providers),
                'series_count': signals['series_count'],
                'clarification_detected': signals['clarification_detected'],
                'error': data.get('error'),
                'reasons': reasons,
                'attempt_count': attempt_count,
            }
        results.append(result)
        if result['viability_pass']:
            kept_rows.append(row)
        else:
            rejected_rows.append(row)
            for reason in result['reasons'] or ['unknown']:
                reason_counts[reason] += 1

    payload = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'dataset_path': str(args.dataset.resolve()),
        'base_url': args.base_url,
        'summary': {
            'session_count': len(results),
            'viability_pass_count': sum(1 for item in results if item['viability_pass']),
            'viability_fail_count': sum(1 for item in results if not item['viability_pass']),
            'reason_counts': dict(reason_counts),
        },
        'results': results,
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    if args.kept_output is not None:
        args.kept_output.resolve().write_text(''.join(json.dumps(row) + '\n' for row in kept_rows), encoding='utf-8')
    if args.rejected_output is not None:
        args.rejected_output.resolve().write_text(''.join(json.dumps(row) + '\n' for row in rejected_rows), encoding='utf-8')

    print(json.dumps({'output': str(output), 'viability_pass_count': payload['summary']['viability_pass_count']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
