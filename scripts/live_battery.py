#!/usr/bin/env python3
"""Sequential live verification battery for OpenEcon (run against localhost:3001).

Runs single-shot accuracy probes + multi-round conversations SEQUENTIALLY
(concurrent probes previously caused load-induced false failures).

Usage: python3 live_battery.py [--only PREFIX] [--base http://localhost:3001]
Output: prints a PASS/FAIL table and writes results JSON next to this file.
"""
import argparse
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

BASE = "http://localhost:3001"


def post_query(query, conversation_id=None, timeout=90):
    payload = {"query": query}
    if conversation_id:
        payload["conversationId"] = conversation_id
    req = urllib.request.Request(
        BASE + "/api/query",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
            return body, time.time() - t0, None
    except Exception as e:  # noqa: BLE001
        return None, time.time() - t0, str(e)


def series_points(resp):
    out = []
    for d in resp.get("data") or []:
        pts = d.get("data") or d.get("dataPoints") or []
        out.append((d.get("metadata", {}), pts))
    return out


def check(resp, exp):
    """exp keys: nonempty(bool), provider(str in metadata.source), value_range((lo,hi) on latest pt),
    min_series(int), error_contains(str), clarification(bool), indicator_contains(str)."""
    problems = []
    if resp is None:
        return ["request failed"]
    data = resp.get("data") or []
    if exp.get("nonempty") and not data:
        problems.append(f"empty data (error={resp.get('error')!r})")
    if exp.get("clarification") and not resp.get("clarificationNeeded"):
        problems.append("expected clarification")
    if "error_contains" in exp:
        err = (resp.get("error") or "") + " " + (resp.get("message") or "")
        if exp["error_contains"].lower() not in err.lower():
            problems.append(
                f"error missing {exp['error_contains']!r}: "
                f"error={resp.get('error')!r} message={(resp.get('message') or '')[:80]!r} "
                f"series={len(data)}"
            )
    if "min_series" in exp and len(data) < exp["min_series"]:
        problems.append(f"series {len(data)} < {exp['min_series']}")
    if "provider" in exp and data:
        sources = {(d.get("metadata", {}).get("source") or "").lower() for d in data}
        if not any(exp["provider"].lower() in s for s in sources):
            problems.append(f"provider {sources} != {exp['provider']}")
    if "indicator_contains" in exp and data:
        names = " | ".join((d.get("metadata", {}).get("indicator") or "") for d in data).lower()
        if exp["indicator_contains"].lower() not in names:
            problems.append(f"indicator {names[:100]!r} missing {exp['indicator_contains']!r}")
    if "value_range" in exp and data:
        lo, hi = exp["value_range"]
        pts = data[0].get("data") or data[0].get("dataPoints") or []
        if not pts:
            problems.append("no points on first series")
        else:
            v = pts[-1].get("value")
            if v is None or not (lo <= v <= hi):
                problems.append(f"latest value {v} not in [{lo},{hi}]")
    return problems


# ---------------- single-shot probes ----------------
SINGLE = [
    # English no-regression (red-team protocol A)
    ("en-unrate", "US unemployment rate", {"nonempty": True, "value_range": (2.5, 8)}),
    ("en-gdp", "US GDP", {"nonempty": True, "provider": "fred"}),
    ("en-jobs", "jobs numbers for the US", {"nonempty": True}),
    ("en-m2", "US M2 money supply", {"nonempty": True, "indicator_contains": "m2"}),
    ("en-payrolls", "nonfarm payrolls", {"nonempty": True, "indicator_contains": "nonfarm"}),
    ("en-yield", "10 year treasury yield", {"nonempty": True, "value_range": (2, 7)}),
    ("en-de-cpi", "Germany inflation rate", {"nonempty": True, "value_range": (-1, 9)}),
    ("en-brl", "Brazil exchange rate", {"nonempty": True, "value_range": (3, 8)}),
    ("en-ca-unemp", "Canada unemployment rate", {"nonempty": True, "value_range": (3, 10)}),
    ("en-btc", "Bitcoin price", {"nonempty": True, "value_range": (10000, 500000)}),
    # Chinese battery (red-team protocols A/C)
    ("zh-unemp", "中国失业率", {"nonempty": True, "value_range": (3, 7)}),
    ("zh-cpi5y", "中国CPI 近5年", {"nonempty": True}),
    ("zh-usdcny", "美元兑人民币汇率", {"nonempty": True, "value_range": (6, 8.5)}),
    ("zh-ca-unemp", "加拿大失业率", {"nonempty": True}),
    ("zh-multi", "俄罗斯 GDP总量 GDP增长率 人均GDP", {"nonempty": True, "min_series": 2}),
    # Subnational fail-closed (protocol B): after wave-2 expect explicit national-only explanation
    ("zh-beijing", "北京GDP", {}),  # inspect manually: must NOT be national data labeled Beijing
    ("en-ontario", "Ontario unemployment rate", {"nonempty": True}),  # must NOT regress
    ("en-calif", "California GDP", {"nonempty": True}),  # must NOT regress
    ("en-georgia", "Georgia GDP", {}),  # false-positive guard, inspect
    # Provider fixes
    ("bis-credit", "total credit to the non-financial sector for Germany", {"nonempty": True}),
    ("ct-veg-oil", "US exports of vegetable oil", {}),  # must NOT be HS27 petroleum; fail-closed OK
    ("eu-ea-hicp", "euro area inflation rate", {"nonempty": True, "value_range": (-1, 9)}),
    ("wb-empty-note", "GDP of Tuvalu since 2030", {"error_contains": "no data"}),  # silent-empty fix
]

# ---------------- multi-round conversations (10, per mandate) ----------------
MULTI = [
    ("mr1-country-switch", [
        ("US GDP per capita", {"nonempty": True}),
        ("what about Canada", {"nonempty": True, "value_range": (30000, 80000)}),
    ]),
    ("mr2-indicator-switch", [
        ("Germany inflation", {"nonempty": True}),
        ("show unemployment instead", {"nonempty": True, "indicator_contains": "unemploy"}),
    ]),
    ("mr3-frequency", [
        ("US GDP annual", {"nonempty": True}),
        ("make it quarterly", {"nonempty": True}),  # F1: must re-resolve, quarterly pts
    ]),
    ("mr4-time-window", [
        ("France GDP from 2000 to 2015", {"nonempty": True}),
        ("since 2020", {"nonempty": True}),
    ]),
    ("mr5-fallback-provenance", [
        ("Canada M2 money supply", {}),  # likely fallback-served; F2: follow-up must not break
        ("what about France", {}),
    ]),
    ("mr6-add-country", [
        ("Italy government debt to GDP", {"nonempty": True}),
        ("compare with Spain", {"nonempty": True, "min_series": 2}),
    ]),
    ("mr7-cross-language", [
        ("中国失业率", {"nonempty": True}),
        ("现在看GDP", {"nonempty": True}),  # protocol A: no false topic reset
    ]),
    ("mr8-dimension-switch", [
        ("Canada unemployment rate by sex", {"nonempty": True}),
        ("show GDP instead", {"nonempty": True}),  # F3: no Sex dimension leak
    ]),
    ("mr9-eurostat-switch", [
        ("euro area inflation", {"nonempty": True}),
        ("what about France", {"nonempty": True}),
        ("and Italy", {"nonempty": True}),
    ]),
    ("mr10-fx-follow", [
        ("USD to EUR exchange rate", {"nonempty": True}),
        ("in yen instead", {"nonempty": True}),
    ]),
]


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--base", default="http://localhost:3001")
    ap.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args()
    BASE = args.base

    results = []
    for name, q, exp in SINGLE:
        if args.only and not name.startswith(args.only):
            continue
        resp, dt, err = post_query(q)
        problems = [err] if err else check(resp, exp)
        results.append({"test": name, "query": q, "ok": not problems, "problems": problems,
                        "secs": round(dt, 1),
                        "error": (resp or {}).get("error"), "n_series": len((resp or {}).get("data") or [])})
        print(f"[{'PASS' if not problems else 'FAIL'}] {name} ({dt:.1f}s) {problems or ''}")
        time.sleep(args.sleep)

    for name, turns in MULTI:
        if args.only and not name.startswith(args.only):
            continue
        cid = f"battery-{name}-{uuid.uuid4().hex[:8]}"
        for i, (q, exp) in enumerate(turns):
            resp, dt, err = post_query(q, conversation_id=cid)
            problems = [err] if err else check(resp, exp)
            results.append({"test": f"{name}.t{i+1}", "query": q, "ok": not problems,
                            "problems": problems, "secs": round(dt, 1),
                            "error": (resp or {}).get("error"),
                            "n_series": len((resp or {}).get("data") or [])})
            print(f"[{'PASS' if not problems else 'FAIL'}] {name}.t{i+1} ({dt:.1f}s) {problems or ''}")
            time.sleep(args.sleep)

    out = Path(__file__).with_name("battery_results.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    n_ok = sum(1 for r in results if r["ok"])
    print(f"\n{n_ok}/{len(results)} passed -> {out}")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
