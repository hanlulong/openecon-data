#!/usr/bin/env python3
"""Canonical multiround gate wrapper used by Phase 6 and later cycles."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def resolve_python() -> Path:
    candidates = [
        ROOT / "backend" / ".venv" / "bin" / "python",
        Path("/home/hanlulong/OpenEcon/backend/.venv/bin/python"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No backend virtualenv python found for multiround wrapper")


PYTHON = resolve_python()
HARNESS = ROOT / "scripts" / "test_multiround_10x10.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:3001")
    parser.add_argument("--report", required=True)
    parser.add_argument("--suite", default="baseline")
    parser.add_argument("--min-effective-rate", type=float, default=0.90)
    parser.add_argument("--max-fails", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--request-timeout", type=int, default=60)
    parser.add_argument("--round-delay-seconds", type=float, default=0.5)
    parser.add_argument("--between-test-delay-seconds", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=1)
    args = parser.parse_args()

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["OPENECON_MULTIROUND_BASE_URL"] = str(args.base_url).rstrip("/")
    env["OPENECON_MULTIROUND_REPORT"] = str(report_path)
    env["OPENECON_MULTIROUND_SUITE"] = str(args.suite)
    env["OPENECON_MULTIROUND_MIN_EFFECTIVE_RATE"] = str(args.min_effective_rate)
    env["OPENECON_MULTIROUND_MAX_FAILS"] = str(args.max_fails)
    env["OPENECON_MULTIROUND_REQUEST_TIMEOUT"] = str(args.request_timeout)
    env["OPENECON_MULTIROUND_ROUND_DELAY_SECONDS"] = str(args.round_delay_seconds)
    env["OPENECON_MULTIROUND_BETWEEN_TEST_DELAY_SECONDS"] = str(args.between_test_delay_seconds)
    env["OPENECON_MULTIROUND_MAX_RETRIES"] = str(args.max_retries)

    cmd = [
        str(PYTHON),
        str(HARNESS),
        "--base-url",
        str(args.base_url),
        "--report",
        str(report_path),
        "--suite",
        str(args.suite),
        "--min-effective-rate",
        str(args.min_effective_rate),
        "--max-fails",
        str(args.max_fails),
        "--request-timeout",
        str(args.request_timeout),
        "--round-delay-seconds",
        str(args.round_delay_seconds),
        "--between-test-delay-seconds",
        str(args.between_test_delay_seconds),
        "--max-retries",
        str(args.max_retries),
    ]
    timed_out = False
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=args.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        result = subprocess.CompletedProcess(
            cmd,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + "\nTimed out waiting for multiround harness.",
        )

    wrapper_report = {
        "ok": result.returncode == 0,
        "cmd": " ".join(cmd),
        "base_url": str(args.base_url).rstrip("/"),
        "report_path": str(report_path),
        "timed_out": timed_out,
        "timeout_seconds": args.timeout_seconds,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
    wrapper_path = report_path.with_suffix(".wrapper.json")
    wrapper_path.write_text(json.dumps(wrapper_report, indent=2) + "\n", encoding="utf-8")

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
