#!/usr/bin/env python3
"""
Multi-round conversation test: 10 tests x 10 rounds = 100 total rounds.
Tests the most difficult patterns: provider switching, indicator variants,
country add/remove, dimension changes, and mixed provider stress.
"""

import argparse
import json
import os
import requests
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.multiround_suites import (  # noqa: E402
    DEFAULT_SUITE_NAME,
    get_suite_description,
    list_suite_descriptions,
    load_suite,
)

REPORT_DIR = ROOT / "docs" / "testing" / "reports"
BASE_URL = os.environ.get("OPENECON_MULTIROUND_BASE_URL", "http://localhost:3001").rstrip("/")
TIMEOUT = 90
DEFAULT_REPORT_PATH = os.environ.get("OPENECON_MULTIROUND_REPORT")
DEFAULT_SUITE = os.environ.get("OPENECON_MULTIROUND_SUITE", DEFAULT_SUITE_NAME)
MIN_EFFECTIVE_RATE = float(os.environ.get("OPENECON_MULTIROUND_MIN_EFFECTIVE_RATE", "0.90"))
MAX_FAILS = int(os.environ.get("OPENECON_MULTIROUND_MAX_FAILS", "0"))
REQUEST_TIMEOUT = int(os.environ.get("OPENECON_MULTIROUND_REQUEST_TIMEOUT", str(TIMEOUT)))
MAX_RETRIES = int(os.environ.get("OPENECON_MULTIROUND_MAX_RETRIES", "2"))
RETRY_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_RETRY_DELAY_SECONDS", "3"))
CONNECTION_RETRY_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_CONNECTION_RETRY_DELAY_SECONDS", "5"))
ROUND_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_ROUND_DELAY_SECONDS", "2"))
BETWEEN_TEST_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_BETWEEN_TEST_DELAY_SECONDS", "3"))
HEALTH_RETRIES = int(os.environ.get("OPENECON_MULTIROUND_HEALTH_RETRIES", "3"))
HEALTH_RETRY_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_HEALTH_RETRY_DELAY_SECONDS", "5"))


def classify_response(resp_json):
    """Classify a response as PASS, FAIL, CLARIFY, or WARN."""
    if not resp_json:
        return "FAIL", "no_response", ""

    # Check for error
    error = resp_json.get("error")
    if error:
        error_str = str(error).lower()
        if "clarif" in error_str or "ambiguous" in error_str or "did you mean" in error_str:
            return "CLARIFY", "clarification_needed", ""
        return "FAIL", f"error: {str(error)[:60]}", ""

    # Check for clarification in response
    resp_text = resp_json.get("response", "")
    if isinstance(resp_text, str):
        resp_lower = resp_text.lower()
        if any(w in resp_lower for w in ["could you clarify", "did you mean", "please specify", "which specific", "ambiguous"]):
            return "CLARIFY", "clarification_in_response", ""

    # Check for data
    data = resp_json.get("data")
    source = resp_json.get("source", resp_json.get("provider", ""))

    # Try to get indicator
    indicator = ""
    if isinstance(data, dict):
        indicator = data.get("series_id", data.get("indicator", data.get("indicator_id", "")))
        if not indicator:
            metadata = data.get("metadata", {})
            if isinstance(metadata, dict):
                indicator = metadata.get("series_id", metadata.get("indicator", ""))
    if not indicator:
        indicator = resp_json.get("indicator", resp_json.get("series_id", ""))

    # Check for actual data points
    has_data = False
    if isinstance(data, dict):
        # Check for various data structures
        values = data.get("values", data.get("observations", data.get("data", [])))
        if isinstance(values, list) and len(values) > 0:
            has_data = True
        # Check datasets (multi-series)
        datasets = data.get("datasets", [])
        if isinstance(datasets, list) and len(datasets) > 0:
            has_data = True
        # Check for time_series
        ts = data.get("time_series", data.get("timeSeries", []))
        if isinstance(ts, (list, dict)) and len(ts) > 0:
            has_data = True
        # Check for chart_data
        cd = data.get("chart_data", data.get("chartData", []))
        if isinstance(cd, list) and len(cd) > 0:
            has_data = True
    elif isinstance(data, list) and len(data) > 0:
        has_data = True

    # Also check top-level results/datasets
    if not has_data:
        results = resp_json.get("results", resp_json.get("datasets", []))
        if isinstance(results, list) and len(results) > 0:
            has_data = True

    # Check for chart config (sometimes data is embedded there)
    if not has_data:
        chart = resp_json.get("chart", resp_json.get("chartConfig", {}))
        if isinstance(chart, dict) and chart:
            has_data = True

    if has_data:
        return "PASS", str(source)[:30], str(indicator)[:20]

    # If there's a text response but no data, mark as WARN
    if resp_text and len(str(resp_text)) > 20:
        return "WARN", str(source)[:30] if source else "text_only", str(indicator)[:20]

    return "FAIL", "no_data", str(indicator)[:20]


def run_round(query, conversation_id=None, max_retries=MAX_RETRIES, request_timeout=REQUEST_TIMEOUT):
    """Send a single query and return (response_json, conversation_id, elapsed)."""
    payload = {"query": query}
    if conversation_id:
        payload["conversationId"] = conversation_id

    for attempt in range(max_retries + 1):
        start = time.time()
        try:
            resp = requests.post(
                f"{BASE_URL}/api/query",
                json=payload,
                timeout=request_timeout,
                headers={"Content-Type": "application/json"},
            )
            elapsed = time.time() - start
            if resp.status_code != 200:
                if attempt < max_retries:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                return {"error": f"HTTP {resp.status_code}: {resp.text[:100]}"}, conversation_id, elapsed
            data = resp.json()
            # Extract conversationId
            cid = data.get("conversationId", data.get("conversation_id", conversation_id))
            return data, cid, elapsed
        except requests.exceptions.Timeout:
            elapsed = time.time() - start
            if attempt < max_retries:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return {"error": "TIMEOUT"}, conversation_id, elapsed
        except (requests.exceptions.ConnectionError, ConnectionError) as e:
            elapsed = time.time() - start
            if attempt < max_retries:
                print(f"       [retry {attempt+1}] Connection error, waiting {CONNECTION_RETRY_DELAY_SECONDS}s...")
                time.sleep(CONNECTION_RETRY_DELAY_SECONDS)
                # Check if server is back
                try:
                    requests.get(f"{BASE_URL}/api/health", timeout=5)
                except:
                    time.sleep(CONNECTION_RETRY_DELAY_SECONDS)  # Extra wait
                continue
            return {"error": str(e)[:100]}, conversation_id, elapsed
        except Exception as e:
            elapsed = time.time() - start
            if attempt < max_retries:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return {"error": str(e)[:100]}, conversation_id, elapsed
    return {"error": "max retries exhausted"}, conversation_id, 0


def run_test(test_name, queries):
    """Run a full 10-round test."""
    print(f"\n{'='*80}")
    print(f"  {test_name}")
    print(f"{'='*80}")

    results = []
    conversation_id = None

    for i, query in enumerate(queries, 1):
        print(f"\n  R{i:2d}: {query}")
        resp_json, conversation_id, elapsed = run_round(query, conversation_id)
        status, source, indicator = classify_response(resp_json)

        symbol = {"PASS": "+", "FAIL": "X", "CLARIFY": "?", "WARN": "~"}[status]
        print(f"       [{symbol}] {status:7s} | src={source:20s} | ind={indicator:20s} | {elapsed:.1f}s")

        results.append({
            "round": i,
            "query": query,
            "status": status,
            "source": source,
            "indicator": indicator,
            "elapsed": elapsed,
            "conversation_id": conversation_id,
        })

        # Delay between rounds to avoid overwhelming the server
        time.sleep(ROUND_DELAY_SECONDS)

    return results


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--suite", default=DEFAULT_SUITE)
    parser.add_argument("--list-suites", action="store_true")
    parser.add_argument("--min-effective-rate", type=float, default=MIN_EFFECTIVE_RATE)
    parser.add_argument("--max-fails", type=int, default=MAX_FAILS)
    parser.add_argument("--request-timeout", type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--round-delay-seconds", type=float, default=ROUND_DELAY_SECONDS)
    parser.add_argument("--between-test-delay-seconds", type=float, default=BETWEEN_TEST_DELAY_SECONDS)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    return parser.parse_args()


def main():
    args = parse_args()
    if args.list_suites:
        for suite_name, description in list_suite_descriptions().items():
            print(f"{suite_name}: {description}")
        return

    base_url = str(args.base_url).rstrip("/")
    global BASE_URL, REQUEST_TIMEOUT, ROUND_DELAY_SECONDS, BETWEEN_TEST_DELAY_SECONDS, MAX_RETRIES
    BASE_URL = base_url
    REQUEST_TIMEOUT = args.request_timeout
    ROUND_DELAY_SECONDS = args.round_delay_seconds
    BETWEEN_TEST_DELAY_SECONDS = args.between_test_delay_seconds
    MAX_RETRIES = args.max_retries
    tests = load_suite(args.suite)
    suite_description = get_suite_description(args.suite)

    print(f"Multi-Round Conversation Test Suite")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target: {base_url}")
    print(f"Suite: {args.suite} — {suite_description}")
    print(f"Tests: {len(tests)} x 10 rounds = {sum(len(v) for v in tests.values())} total rounds")

    # Verify backend is up
    try:
        health = requests.get(f"{base_url}/api/health", timeout=30)
        if health.status_code != 200:
            print(f"\nERROR: Backend health check failed: {health.status_code}")
            sys.exit(1)
        print(f"Backend: healthy")
    except Exception as e:
        print(f"\nERROR: Backend not reachable: {e}")
        sys.exit(1)

    all_results = {}
    overall_start = time.time()

    for idx, (test_name, queries) in enumerate(tests.items()):
        # Check backend health between tests
        if idx > 0:
            print(f"\n  ... waiting {BETWEEN_TEST_DELAY_SECONDS}s between tests ...")
            time.sleep(BETWEEN_TEST_DELAY_SECONDS)
            for retry in range(HEALTH_RETRIES):
                try:
                    h = requests.get(f"{base_url}/api/health", timeout=30)
                    if h.status_code == 200:
                        break
                except:
                    pass
                print(f"  ... backend not ready, waiting {HEALTH_RETRY_DELAY_SECONDS}s (retry {retry+1}) ...")
                time.sleep(HEALTH_RETRY_DELAY_SECONDS)
        all_results[test_name] = run_test(test_name, queries)

    total_elapsed = time.time() - overall_start

    # ── Summary ─────────────────────────────────────────────────────────────
    print(f"\n\n{'='*100}")
    print(f"  SUMMARY")
    print(f"{'='*100}")
    print(f"\n{'Test':<40s} {'PASS':>5s} {'WARN':>5s} {'CLAR':>5s} {'FAIL':>5s} {'Rate':>7s} {'AvgTime':>8s}")
    print(f"{'-'*40} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*7} {'-'*8}")

    total_pass = 0
    total_warn = 0
    total_clarify = 0
    total_fail = 0
    total_rounds = 0

    for test_name, results in all_results.items():
        p = sum(1 for r in results if r["status"] == "PASS")
        w = sum(1 for r in results if r["status"] == "WARN")
        c = sum(1 for r in results if r["status"] == "CLARIFY")
        f = sum(1 for r in results if r["status"] == "FAIL")
        avg_t = sum(r["elapsed"] for r in results) / len(results)
        rate = f"{(p+w)*100/len(results):.0f}%"

        total_pass += p
        total_warn += w
        total_clarify += c
        total_fail += f
        total_rounds += len(results)

        # Shorten test name for display
        short_name = test_name[:39]
        print(f"{short_name:<40s} {p:5d} {w:5d} {c:5d} {f:5d} {rate:>7s} {avg_t:7.1f}s")

    print(f"{'-'*40} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*7} {'-'*8}")
    overall_rate = f"{(total_pass+total_warn)*100/total_rounds:.0f}%"
    avg_overall = total_elapsed / total_rounds
    print(f"{'TOTAL':<40s} {total_pass:5d} {total_warn:5d} {total_clarify:5d} {total_fail:5d} {overall_rate:>7s} {avg_overall:7.1f}s")

    print(f"\nTotal time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    print(f"Effective rate (PASS+WARN): {(total_pass+total_warn)}/{total_rounds} = {overall_rate}")
    print(f"Strict PASS rate: {total_pass}/{total_rounds} = {total_pass*100/total_rounds:.0f}%")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ── Detailed failures ───────────────────────────────────────────────────
    failures = []
    for test_name, results in all_results.items():
        for r in results:
            if r["status"] == "FAIL":
                failures.append((test_name, r))

    if failures:
        print(f"\n\n{'='*100}")
        print(f"  FAILURES ({len(failures)})")
        print(f"{'='*100}")
        for test_name, r in failures:
            short_test = test_name.split(":")[1].strip()[:20] if ":" in test_name else test_name[:20]
            print(f"  [{short_test}] R{r['round']:2d}: {r['query']}")
            print(f"         source={r['source']}")

    # ── Save JSON report ────────────────────────────────────────────────────
    effective_rate_ratio = (total_pass + total_warn) / total_rounds
    strict_pass_rate_ratio = total_pass / total_rounds
    ok = total_fail <= args.max_fails and effective_rate_ratio >= args.min_effective_rate

    report = {
        "timestamp": datetime.now().isoformat(),
        "base_url": base_url,
        "suite": args.suite,
        "suite_description": suite_description,
        "total_rounds": total_rounds,
        "pass": total_pass,
        "warn": total_warn,
        "clarify": total_clarify,
        "fail": total_fail,
        "effective_rate": f"{effective_rate_ratio*100:.1f}%",
        "effective_rate_ratio": round(effective_rate_ratio, 4),
        "strict_pass_rate": f"{strict_pass_rate_ratio*100:.1f}%",
        "strict_pass_rate_ratio": round(strict_pass_rate_ratio, 4),
        "min_effective_rate": args.min_effective_rate,
        "max_fails": args.max_fails,
        "ok": ok,
        "total_time_seconds": round(total_elapsed, 1),
        "tests": {},
    }
    for test_name, results in all_results.items():
        report["tests"][test_name] = results

    if args.report:
        report_path = args.report
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if args.suite == DEFAULT_SUITE_NAME:
            filename = f"multiround_10x10_{timestamp}.json"
        else:
            filename = f"multiround_10x10_{args.suite}_{timestamp}.json"
        report_path = str(REPORT_DIR / filename)
    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved: {report_path}")
    except Exception as e:
        print(f"\nFailed to save report: {e}")

    if not ok:
        print(
            f"\nFAIL: effective_rate={effective_rate_ratio:.3f} "
            f"(min {args.min_effective_rate:.3f}), fails={total_fail} (max {args.max_fails})"
        )
        sys.exit(1)

    print(
        f"\nPASS: effective_rate={effective_rate_ratio:.3f} "
        f"(min {args.min_effective_rate:.3f}), fails={total_fail} (max {args.max_fails})"
    )


if __name__ == "__main__":
    main()
