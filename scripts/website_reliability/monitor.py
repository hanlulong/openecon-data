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
MCP_DIAGNOSTIC_CODES = {
    "capacity_rejected", "http_error", "head_did_not_return_405", "query_data_missing",
    "verification_timeout", "transport_error", "protocol_error", "unexpected_error",
    "probe_failed", "probe_timeout", "invalid_probe_output",
}


def safe_mcp_diagnostic(value):
    """Copy only known diagnostic values from a child or saved report."""
    if not isinstance(value, dict):
        return {"code": "probe_failed"}
    code = value.get("code")
    result = {"code": code if isinstance(code, str) and code in MCP_DIAGNOSTIC_CODES else "probe_failed"}
    for key, allowed in (
        ("stage", {"head", "admission", "messages", "discovery", "session"}),
        ("method", {"HEAD", "GET", "POST"}),
    ):
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    status = value.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    retry = value.get("retry_after_seconds")
    if type(retry) is int and 0 <= retry <= 86400:
        result["retry_after_seconds"] = retry
    return result


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
    body = None
    try:
        body = json.loads(result["output"])
        ok = isinstance(body, dict) and result["exit_code"] == 0 and not result["timed_out"] and all(body.get(k) is True for k in (
            "ok", "initialized", "tools_listed", "query_data_available", "session_closed",
        )) and body.get("head_status") == 405 and not any(k in body for k in ("diagnostic", "error", "error_type"))
    except (ValueError, AttributeError):
        ok = False
    summary = {"ok": ok, "reason": "verified" if ok else "mcp_handshake_or_discovery_failed", "timed_out": result["timed_out"]}
    if not ok:
        if result["timed_out"]:
            summary["diagnostic"] = {"code": "probe_timeout"}
        elif not isinstance(body, dict):
            summary["diagnostic"] = {"code": "invalid_probe_output"}
        else:
            summary["diagnostic"] = safe_mcp_diagnostic(body.get("diagnostic"))
    return summary


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


def failure_details(checks):
    """Build concise failure details without reflecting arbitrary child text."""
    details = {}
    for name, check in checks.items():
        if check.get("ok") is not False:
            continue
        if name == "mcp":
            diagnostic = {"code": "probe_timeout"} if check.get("timed_out") is True else safe_mcp_diagnostic(check.get("diagnostic"))
            details[name] = {"diagnostic": diagnostic}
        elif name == "websites":
            reason = check.get("reason")
            allowed = {"website_probe_timeout", "website_probe_failed", "invalid_or_incomplete_website_results"}
            details[name] = {"reason": reason if isinstance(reason, str) and reason in allowed else "probe_failed"}
            try:
                details[name]["failed_checks"] = website_summary(check)["failed_checks"]
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        elif name == "backends":
            rows = check.get("checks", [])
            ports = sorted({row.get("port") for row in rows if isinstance(row, dict) and type(row.get("port")) is int and row["port"] in (3001, 3002) and row.get("ok") is False}) if isinstance(rows, list) else []
            details[name] = {"failed_ports": ports} if ports else {"reason": "probe_failed"}
    return details


def saved_status(path):
    """Report fresh failures accurately; incomplete/stale data cannot certify health."""
    failure = lambda reason: {"status": "FAILED", "reason": reason}
    try:
        report = json.loads(path.read_text())
    except FileNotFoundError:
        return failure("missing_report")
    except (OSError, ValueError):
        return failure("invalid_report")
    try:
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            raise ValueError("invalid_schema")
        if report.get("status") == "RUNNING":
            return failure("incomplete_report")
        checked_at = datetime.datetime.fromisoformat(report["checked_at"])
        age = (datetime.datetime.now(datetime.timezone.utc) - checked_at).total_seconds()
        if age > 600:
            return failure("stale_report")
        if age < -60 or report.get("status") not in ("HEALTHY", "DEGRADED", "FAILED"):
            raise ValueError("invalid_time_or_status")
        checks = report["checks"]
        if not isinstance(checks, dict) or set(checks) != {"websites", "mcp", "backends"}:
            raise ValueError("missing_checks")
        if not all(isinstance(check, dict) and type(check.get("ok")) is bool for check in checks.values()):
            raise ValueError("invalid_check_status")
        failed = [name for name, check in checks.items() if not check["ok"]]
        if not isinstance(report["failed_checks"], list) or sorted(report["failed_checks"]) != sorted(failed):
            raise ValueError("contradictory_failed_checks")
        website_check = checks["websites"]
        degraded = []
        if website_check["ok"] or "results" in website_check:
            websites = website_summary(website_check)
            degraded = websites["known_degraded"]
            if website_check["ok"] and not websites["ok"]:
                raise ValueError("contradictory_website_check")
        expected_status = "FAILED" if failed else "DEGRADED" if degraded else "HEALTHY"
        if report["status"] != expected_status or report["known_degraded"] != degraded:
            raise ValueError("contradictory_website_status")
        backend_check = checks["backends"]
        if backend_check["ok"] or "checks" in backend_check:
            backends = backend_check["checks"]
            if not isinstance(backends, list) or len(backends) != 2 or {row["port"] for row in backends} != {3001, 3002}:
                raise ValueError("incomplete_backend_checks")
            if not all(type(row.get("ok")) is bool for row in backends):
                raise ValueError("invalid_backend_check")
            if backend_check["ok"] != all(row["ok"] for row in backends):
                raise ValueError("contradictory_backend_check")
        mcp = checks["mcp"]
        if mcp["ok"] and (mcp.get("timed_out") is not False or mcp.get("reason") != "verified" or "diagnostic" in mcp):
            raise ValueError("contradictory_backend_status")
        result = {k: report[k] for k in ("status", "checked_at", "failed_checks", "known_degraded")}
        if failed:
            result.update(reason="checks_failed", failure_details=failure_details(checks))
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return failure("invalid_report")


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
    summary = {k: report[k] for k in ("status", "checked_at", "duration_seconds", "failed_checks", "known_degraded")}
    if failed:
        summary["failure_details"] = failure_details(report["checks"])
    print(json.dumps(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
