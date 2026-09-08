#!/usr/bin/env python3
"""Bounded local availability monitoring; no tool queries or service mutations."""

import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

from check_websites import JOBS, KNOWN_DOWN

HERE = Path(__file__).resolve().parent


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".monitor-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_probe(arguments, timeout):
    process = subprocess.Popen(
        arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        return {"exit_code": process.returncode, "output": output, "timed_out": False}
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)
        return {"exit_code": None, "output": "", "timed_out": True}


def website_summary(report):
    if not isinstance(report, dict):
        raise ValueError("invalid_website_report")
    rows = report.get("results", [])
    if not isinstance(rows, list) or len(rows) != len(JOBS):
        raise ValueError("incomplete_website_results")
    observed = {(r["host"], r["scheme"], r["ip_family"]) for r in rows}
    if observed != JOBS:
        raise ValueError("incorrect_website_inventory")
    failed = []
    degraded = set()
    for row in rows:
        good_transport = (
            row.get("curl_exit_code") == 0 and row.get("tls_verified") is True
            and row.get("canonical_target_verified") is True
        )
        known = row["host"] in KNOWN_DOWN and row.get("http_status") == 503
        if not good_transport or (row.get("http_status") != 200 and not known):
            failed.append(f"{row['host']}:{row['scheme']}:IPv{row['ip_family']}")
        elif known:
            degraded.add(row["host"])
    return {"ok": not failed, "failed_checks": failed, "known_degraded": sorted(degraded), "results": rows}


def check_websites(work):
    output = work / "websites.json"
    result = run_probe([sys.executable, str(HERE / "check_websites.py"), "--output", str(output)], 245)
    if result["timed_out"]:
        return {"ok": False, "reason": "website_probe_timeout", "complete": False}
    try:
        summary = website_summary(json.loads(output.read_text()))
        if result["exit_code"] not in (0, 1):
            raise ValueError("website_probe_crashed")
        if result["exit_code"] != 0:
            summary.update({"ok": False, "reason": "website_probe_failed"})
        return summary
    except (OSError, ValueError, TypeError, KeyError):
        return {"ok": False, "reason": "invalid_or_incomplete_website_results", "complete": False}


def check_mcp():
    result = run_probe([sys.executable, str(HERE / "check_mcp_session.py"), "https://data.openecon.ai/mcp"], 30)
    try:
        body = json.loads(result["output"])
        ok = result["exit_code"] == 0 and all(body.get(k) is True for k in (
            "ok", "initialized", "tools_listed", "query_data_available", "session_closed",
        )) and body.get("head_status") == 405
    except (ValueError, AttributeError):
        ok = False
    return {"ok": ok, "reason": "verified" if ok else "mcp_handshake_or_discovery_failed", "timed_out": result["timed_out"]}


def check_backends():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    checks = []
    for port in (3001, 3002):
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
                payload = json.loads(response.read(1024 * 1024))
                ok = response.status == 200 and payload.get("status") == "ok"
            checks.append({"port": port, "ok": ok})
        except Exception as error:
            checks.append({"port": port, "ok": False, "error_type": type(error).__name__})
    return {"ok": all(check["ok"] for check in checks), "checks": checks}


def saved_status(path):
    """Incomplete or older-than-ten-minute reports cannot certify availability."""
    try:
        report = json.loads(path.read_text())
        checked_at = datetime.datetime.fromisoformat(report["checked_at"])
        age = (datetime.datetime.now(datetime.timezone.utc) - checked_at).total_seconds()
        if report.get("schema_version") != 1 or not -60 <= age <= 600:
            raise ValueError("stale_or_invalid_report")
        if report.get("status") not in ("HEALTHY", "DEGRADED"):
            raise ValueError("failed_or_incomplete_report")
        checks = report["checks"]
        if set(checks) != {"websites", "mcp", "backends"} or report["failed_checks"]:
            raise ValueError("missing_or_failed_checks")
        if not all(check.get("ok") is True for check in checks.values()):
            raise ValueError("contradictory_check_status")
        websites = website_summary(checks["websites"])
        degraded = websites["known_degraded"]
        expected_status = "DEGRADED" if degraded else "HEALTHY"
        if not websites["ok"] or report["status"] != expected_status or report["known_degraded"] != degraded:
            raise ValueError("contradictory_website_status")
        backends = checks["backends"]["checks"]
        if len(backends) != 2 or {row["port"] for row in backends} != {3001, 3002}:
            raise ValueError("incomplete_backend_checks")
        if not all(row.get("ok") is True for row in backends) or checks["mcp"].get("timed_out") is not False:
            raise ValueError("contradictory_backend_status")
        return {k: report[k] for k in ("status", "checked_at", "failed_checks", "known_degraded")}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "FAILED", "reason": "missing_stale_or_incomplete_report"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--status", action="store_true", help="Read a saved result and verify freshness without probing")
    args = parser.parse_args()
    if args.status:
        result = saved_status(args.output)
        print(json.dumps(result))
        return 1 if result["status"] == "FAILED" else 0
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "RUNNING", "checks": {},
    }
    # A killed run leaves an explicit incomplete report instead of old success.
    atomic_json(args.output, report)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=args.output.parent) as temporary:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {
                "websites": pool.submit(check_websites, Path(temporary)),
                "mcp": pool.submit(check_mcp),
                "backends": pool.submit(check_backends),
            }
            for name, future in jobs.items():
                try:
                    report["checks"][name] = future.result()
                except Exception as error:
                    report["checks"][name] = {"ok": False, "error_type": type(error).__name__}
    failed = [name for name, check in report["checks"].items() if not check["ok"]]
    degraded = report["checks"]["websites"].get("known_degraded", [])
    report.update({
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 2),
        "status": "FAILED" if failed else "DEGRADED" if degraded else "HEALTHY",
        "failed_checks": failed, "known_degraded": degraded,
    })
    atomic_json(args.output, report)
    print(json.dumps({k: report[k] for k in ("status", "checked_at", "duration_seconds", "failed_checks", "known_degraded")}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
