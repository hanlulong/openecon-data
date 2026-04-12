#!/usr/bin/env python3
"""Phase 2 gate runner for verified-truth controls.

Runs the focused suites that prove the minimal typed execution contract,
post-fetch verification plumbing, and staged-state safeguards are in place.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "backend" / ".venv" / "bin" / "python"


def run_cmd(cmd: list[str]) -> dict[str, object]:
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return {
        "cmd": " ".join(cmd),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    suites = [
        run_cmd([
            str(PYTHON),
            "-m",
            "pytest",
            "tests/test_outcome_guarantee_phase2_verified_truth.py",
            "-q",
        ]),
        run_cmd([
            str(PYTHON),
            "-m",
            "pytest",
            "backend/tests/test_query_service.py",
            "-k",
            "verification_failure or staged_commit or ranking_answer_with_single_series",
            "-q",
        ]),
        run_cmd([
            str(PYTHON),
            "-m",
            "pytest",
            "backend/tests/test_conversation_state_v2.py",
            "-q",
        ]),
    ]

    ok = all(item["returncode"] == 0 for item in suites)
    report = {
        "ok": ok,
        "suites": suites,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if ok:
        print(f"Phase 2 gate PASS: {report_path}")
        return 0
    print(f"Phase 2 gate FAIL: {report_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
