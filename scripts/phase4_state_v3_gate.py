#!/usr/bin/env python3
"""Phase 4 gate runner for decomposition/state V3 hardening.

This scoped gate proves that decomposition is first-class in state semantics,
that full breakdowns stay distinct from member filters, and that multiround
state preserves verified decomposition meaning across follow-up changes.
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
    raise FileNotFoundError("No backend virtualenv python found for Phase 4 gate runner")


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
        "# Phase 4 State V3 Report",
        "",
        f"- Overall status: {'PASS' if report.get('ok') else 'FAIL'}",
        f"- Decomposition first-class: {summary.get('decomposition_first_class')}",
        f"- Breakdown/member-filter distinction stable: {summary.get('breakdown_member_filter_distinct')}",
        f"- Multiround state preserved: {summary.get('multiround_state_preserved')}",
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
            "phase4_contract_and_state_tests",
            [
                str(PYTHON),
                "-m",
                "pytest",
                "tests/test_outcome_guarantee_phase4_state_v3.py",
                "-q",
            ],
        ),
        run_cmd(
            "phase4_conversation_state_regressions",
            [
                str(PYTHON),
                "-m",
                "pytest",
                "backend/tests/test_conversation_state_v2.py",
                "-k",
                (
                    "geography_breakdown_promotes_to_first_class_decomposition or "
                    "specific_geography_filter_clears_first_class_decomposition or "
                    "dimension_category_extraction_promotes_to_decomposition or "
                    "preserves_decomposition_on_time_change or "
                    "does_not_preserve_decomposition_when_geo_filter_added"
                ),
                "-q",
            ],
        ),
    ]

    ok = all(suite["returncode"] == 0 for suite in suites)
    summary = {
        "decomposition_first_class": all(
            suite["returncode"] == 0
            for suite in suites
            if suite["name"] == "phase4_contract_and_state_tests"
        ),
        "breakdown_member_filter_distinct": all(
            suite["returncode"] == 0
            for suite in suites
            if suite["name"] == "phase4_conversation_state_regressions"
        ),
        "multiround_state_preserved": all(
            suite["returncode"] == 0
            for suite in suites
            if suite["name"] == "phase4_conversation_state_regressions"
        ),
        "regressions_clear": ok,
    }

    report = {"ok": ok, "summary": summary, "suites": suites}
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_markdown_report(report_path.with_suffix(".md"), report)

    if ok:
        print(f"Phase 4 gate PASS: {report_path}")
        return 0
    print(f"Phase 4 gate FAIL: {report_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
