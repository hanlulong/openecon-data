#!/usr/bin/env python3
"""
Multi-round conversation test: 10 tests x 10 rounds = 100 total rounds.
Tests the most difficult patterns: provider switching, indicator variants,
country add/remove, dimension changes, and mixed provider stress.
"""

import requests
import json
import time
import sys
from datetime import datetime

BASE_URL = "http://localhost:3001"
TIMEOUT = 90

# ── Test definitions ────────────────────────────────────────────────────────

TESTS = {
    "Test 1: GDP Deep Dive": [
        "US GDP",
        "Add China GDP",
        "Add Germany GDP",
        "Switch to per capita GDP",
        "Remove China",
        "Switch to GDP growth rate",
        "Show from IMF instead",
        "Change time range to 2015-2024",
        "Add Japan",
        "Switch to bar chart",
    ],
    "Test 2: Inflation Multi-Provider": [
        "Germany inflation rate",
        "Switch to France inflation",
        "Add Italy inflation",
        "Switch to US inflation from FRED",
        "Add UK inflation",
        "Change to 2020-2025",
        "Switch to World Bank data",
        "Show only US and UK",
        "Switch to core inflation",
        "Change to monthly frequency",
    ],
    "Test 3: Crypto Cycling": [
        "Bitcoin price",
        "Switch to Ethereum price",
        "Switch to Solana price",
        "Back to Bitcoin price last 90 days",
        "Add Ethereum for comparison",
        "Switch to Cardano price",
        "Switch to Dogecoin price",
        "Back to Bitcoin price",
        "Change to last 30 days",
        "Add Ethereum price again",
    ],
    "Test 4: Canada StatsCan Dimensions": [
        "Canada unemployment rate",
        "Show by province",
        "Just Ontario unemployment",
        "Switch to Alberta unemployment",
        "Switch to employment rate",
        "Show by age group",
        "Show 15-24 age group only",
        "Switch back to unemployment rate",
        "Show all provinces",
        "Change to 2020-2025",
    ],
    "Test 5: Trade Data Complex": [
        "US exports to China",
        "Switch to US imports from China",
        "Change partner to Japan",
        "Switch to trade balance US and Japan",
        "Change to Germany exports to China",
        "Add France exports to China",
        "Switch to 2020-2024",
        "Switch back to US exports",
        "Change partner to Canada",
        "Show total trade US and Canada",
    ],
    "Test 6: Exchange Rate Switching": [
        "USD to EUR exchange rate",
        "Switch to USD to GBP",
        "Switch to USD to JPY",
        "Switch to EUR to GBP",
        "Back to USD to EUR",
        "Change to last 30 days",
        "Switch to USD to CAD",
        "Switch to USD to CHF",
        "Back to USD to EUR",
        "Change to last year",
    ],
    "Test 7: BIS + IMF Financial": [
        "BIS credit to GDP ratio",
        "Narrow to US credit to GDP",
        "Add China credit to GDP",
        "Switch to IMF GDP growth rate",
        "Add Germany GDP growth",
        "Switch to current account balance",
        "Change to 2018-2024",
        "Remove Germany",
        "Switch to government debt to GDP",
        "Add Japan government debt",
    ],
    "Test 8: Eurostat Deep Dive": [
        "France unemployment rate from Eurostat",
        "Switch to Germany unemployment",
        "Add Spain unemployment",
        "Switch to inflation rate",
        "Switch to GDP growth rate",
        "Remove Spain",
        "Add Italy",
        "Switch to government debt to GDP",
        "Change to 2015-2024",
        "Switch back to unemployment rate",
    ],
    "Test 9: Mixed Provider Stress": [
        "US GDP from FRED",
        "Japan GDP from World Bank",
        "Germany GDP from Eurostat",
        "China GDP from IMF",
        "Canada GDP from Statistics Canada",
        "Switch all to GDP growth rate",
        "Change to 2020-2025",
        "Show only US and China",
        "Switch to per capita GDP",
        "Add Germany back",
    ],
    "Test 10: Indicator Variant Cycling": [
        "US real GDP",
        "Switch to nominal GDP",
        "Switch to GDP per capita",
        "Switch to GDP growth rate",
        "Switch to GDP deflator",
        "Back to real GDP",
        "Add UK real GDP",
        "Switch to PPP GDP",
        "Change to 2018-2024",
        "Switch to constant prices GDP",
    ],
}


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


def run_round(query, conversation_id=None, max_retries=2):
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
                timeout=TIMEOUT,
                headers={"Content-Type": "application/json"},
            )
            elapsed = time.time() - start
            if resp.status_code != 200:
                if attempt < max_retries:
                    time.sleep(3)
                    continue
                return {"error": f"HTTP {resp.status_code}: {resp.text[:100]}"}, conversation_id, elapsed
            data = resp.json()
            # Extract conversationId
            cid = data.get("conversationId", data.get("conversation_id", conversation_id))
            return data, cid, elapsed
        except requests.exceptions.Timeout:
            elapsed = time.time() - start
            if attempt < max_retries:
                time.sleep(3)
                continue
            return {"error": "TIMEOUT"}, conversation_id, elapsed
        except (requests.exceptions.ConnectionError, ConnectionError) as e:
            elapsed = time.time() - start
            if attempt < max_retries:
                print(f"       [retry {attempt+1}] Connection error, waiting 5s...")
                time.sleep(5)
                # Check if server is back
                try:
                    requests.get(f"{BASE_URL}/api/health", timeout=5)
                except:
                    time.sleep(5)  # Extra wait
                continue
            return {"error": str(e)[:100]}, conversation_id, elapsed
        except Exception as e:
            elapsed = time.time() - start
            if attempt < max_retries:
                time.sleep(3)
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
        time.sleep(2)

    return results


def main():
    print(f"Multi-Round Conversation Test Suite")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target: {BASE_URL}")
    print(f"Tests: {len(TESTS)} x 10 rounds = {sum(len(v) for v in TESTS.values())} total rounds")

    # Verify backend is up
    try:
        health = requests.get(f"{BASE_URL}/api/health", timeout=30)
        if health.status_code != 200:
            print(f"\nERROR: Backend health check failed: {health.status_code}")
            sys.exit(1)
        print(f"Backend: healthy")
    except Exception as e:
        print(f"\nERROR: Backend not reachable: {e}")
        sys.exit(1)

    all_results = {}
    overall_start = time.time()

    for idx, (test_name, queries) in enumerate(TESTS.items()):
        # Check backend health between tests
        if idx > 0:
            print(f"\n  ... waiting 3s between tests ...")
            time.sleep(3)
            for retry in range(3):
                try:
                    h = requests.get(f"{BASE_URL}/api/health", timeout=30)
                    if h.status_code == 200:
                        break
                except:
                    pass
                print(f"  ... backend not ready, waiting 5s (retry {retry+1}) ...")
                time.sleep(5)
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
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_rounds": total_rounds,
        "pass": total_pass,
        "warn": total_warn,
        "clarify": total_clarify,
        "fail": total_fail,
        "effective_rate": f"{(total_pass+total_warn)*100/total_rounds:.1f}%",
        "strict_pass_rate": f"{total_pass*100/total_rounds:.1f}%",
        "total_time_seconds": round(total_elapsed, 1),
        "tests": {},
    }
    for test_name, results in all_results.items():
        report["tests"][test_name] = results

    report_path = "/home/hanlulong/OpenEcon/docs/testing/reports/multiround_10x10_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    try:
        import os
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved: {report_path}")
    except Exception as e:
        print(f"\nFailed to save report: {e}")


if __name__ == "__main__":
    main()
