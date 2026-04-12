#!/usr/bin/env python3
"""Phase 5 gate runner for the provider-migration matrix.

This scoped gate proves that the in-scope migrated providers use the materialized
execution-plan contract and that no silent provider regression is present for the
selected matrix slice.
"""

from __future__ import annotations

import argparse
import json
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
    raise FileNotFoundError("No backend virtualenv python found for Phase 5 gate runner")


PYTHON = resolve_python()


def run_cmd(name: str, cmd: list[str]) -> dict[str, object]:
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return {
        "name": name,
        "cmd": " ".join(cmd),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def write_markdown_report(path: Path, report: dict[str, object]) -> None:
    summary = report.get("summary", {})
    lines = [
        "# Phase 5 Provider Matrix Report",
        "",
        f"- Overall status: {'PASS' if report.get('ok') else 'FAIL'}",
        f"- FRED contract migrated: {summary.get('fred_contract_migrated')}",
        f"- WorldBank contract migrated: {summary.get('worldbank_contract_migrated')}",
        f"- Eurostat contract migrated: {summary.get('eurostat_contract_migrated')}",
        f"- Regressions clear: {summary.get('regressions_clear')}",
        "",
        "## Suites",
    ]
    for suite in report.get("suites", []):
        lines.extend(
            [
                "",
                f"### {suite.get('name')}",
                f"- Status: {'PASS' if suite.get('returncode') == 0 else 'FAIL'}",
                f"- Command: `{suite.get('cmd')}`",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    suites = [
        run_cmd(
            "phase5_contract_matrix_tests",
            [
                str(PYTHON),
                "-m",
                "pytest",
                "tests/test_outcome_guarantee_phase5_provider_matrix.py",
                "-q",
            ],
        ),
        run_cmd(
            "phase5_query_provider_dispatch_tests",
            [
                str(PYTHON),
                "-m",
                "pytest",
                "backend/tests/test_query_service.py",
                "-k",
                (
                    "fetch_data_materializes_fred_provider_contract_for_dispatch or "
                    "fetch_data_materializes_worldbank_provider_contract_for_dispatch or "
                    "fetch_data_materializes_eurostat_provider_contract_for_dispatch or "
                    "build_cache_params_prefers_execution_plan_identity"
                ),
                "-q",
            ],
        ),
    ]

    ok = all(suite["returncode"] == 0 for suite in suites)
    summary = {
        "fred_contract_migrated": all(
            suite["returncode"] == 0 for suite in suites if suite["name"] == "phase5_query_provider_dispatch_tests"
        ),
        "worldbank_contract_migrated": all(
            suite["returncode"] == 0 for suite in suites if suite["name"] == "phase5_query_provider_dispatch_tests"
        ),
        "eurostat_contract_migrated": all(
            suite["returncode"] == 0 for suite in suites if suite["name"] == "phase5_query_provider_dispatch_tests"
        ),
        "regressions_clear": ok,
    }

    report = {"ok": ok, "summary": summary, "suites": suites}
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_markdown_report(report_path.with_suffix(".md"), report)

    if ok:
        print(f"Phase 5 gate PASS: {report_path}")
        return 0
    print(f"Phase 5 gate FAIL: {report_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
