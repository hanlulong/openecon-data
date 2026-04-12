#!/usr/bin/env python3
"""Phase 3 gate runner for the planner/provider-boundary slice.

This scoped gate proves that a materialized execution plan now drives the
provider boundary for the Eurostat path, cache identity can derive from the
plan contract, and the runtime still reuses the same plan object through fetch
and verification.
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
    raise FileNotFoundError("No backend virtualenv python found for Phase 3 gate runner")


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
        "# Phase 3 Planner Boundary Report",
        "",
        f"- Overall status: {'PASS' if report.get('ok') else 'FAIL'}",
        f"- Plan contract materialized: {summary.get('plan_contract_materialized')}",
        f"- Eurostat consumes plan contract: {summary.get('eurostat_consumes_plan_contract')}",
        f"- Cache identity plan-aligned: {summary.get('cache_identity_plan_aligned')}",
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
            "phase3_contract_tests",
            [
                str(PYTHON),
                "-m",
                "pytest",
                "tests/test_outcome_guarantee_phase3_planner_boundary.py",
                "-q",
            ],
        ),
        run_cmd(
            "phase3_query_boundary_tests",
            [
                str(PYTHON),
                "-m",
                "pytest",
                "backend/tests/test_query_service.py",
                "-k",
                (
                    "execute_standard_pipeline_passes_materialized_execution_plan_to_fetch or "
                    "fetch_data_materializes_execution_plan_for_provider_dispatch or "
                    "fetch_data_materializes_eurostat_provider_contract_for_dispatch or "
                    "build_cache_params_prefers_execution_plan_identity"
                ),
                "-q",
            ],
        ),
    ]

    ok = all(suite["returncode"] == 0 for suite in suites)
    summary = {
        "plan_contract_materialized": all(
            suite["returncode"] == 0 for suite in suites if suite["name"] == "phase3_contract_tests"
        ),
        "eurostat_consumes_plan_contract": all(
            suite["returncode"] == 0 for suite in suites if suite["name"] == "phase3_query_boundary_tests"
        ),
        "cache_identity_plan_aligned": all(
            suite["returncode"] == 0 for suite in suites if suite["name"] == "phase3_query_boundary_tests"
        ),
        "regressions_clear": ok,
    }
    report = {"ok": ok, "summary": summary, "suites": suites}

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_markdown_report(report_path.with_suffix(".md"), report)

    if ok:
        print(f"Phase 3 gate PASS: {report_path}")
        return 0
    print(f"Phase 3 gate FAIL: {report_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
