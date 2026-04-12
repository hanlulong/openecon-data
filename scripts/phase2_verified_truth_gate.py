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


def resolve_python() -> Path:
    candidates = [
        ROOT / "backend" / ".venv" / "bin" / "python",
        Path("/home/hanlulong/OpenEcon/backend/.venv/bin/python"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No backend virtualenv python found for Phase 2 gate runner")


PYTHON = resolve_python()


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


def write_markdown_report(report_path: Path, report: dict[str, object]) -> None:
    summary = report.get("summary", {})
    suites = report.get("suites", [])
    lines = [
        "# Phase 2 Verified Truth Report",
        "",
        f"- Overall status: {'PASS' if report.get('ok') else 'FAIL'}",
        f"- Conversation truth guarded: {summary.get('conversation_truth_guarded')}",
        f"- Named false-pass classes blocked: {summary.get('named_false_pass_classes_blocked')}",
        f"- Typed-plan verification proven: {summary.get('typed_plan_verification_proven')}",
        f"- Regression suites clear: {summary.get('regressions_clear')}",
        "",
        "## Suites",
    ]
    for suite in suites:
        status = "PASS" if suite.get("returncode") == 0 else "FAIL"
        lines.extend(
            [
                "",
                f"### {suite.get('name')}",
                f"- Status: {status}",
                f"- Command: `{suite.get('cmd')}`",
            ]
        )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    suite_specs = [
        {
            "name": "phase2_contract_and_typed_plan",
            "cmd": [
                str(PYTHON),
                "-m",
                "pytest",
                "tests/test_outcome_guarantee_phase2_verified_truth.py",
                "-q",
            ],
        },
        {
            "name": "phase2_false_pass_and_standard_path",
            "cmd": [
                str(PYTHON),
                "-m",
                "pytest",
                "backend/tests/test_query_service.py",
                "-k",
                (
                    "ranking_answer_with_single_series or country_scope_mismatch or "
                    "country_scope_without_country_metadata or semantic_judge_is_unavailable or "
                    "rejects_spread_false_pass_via_semantic_judge or "
                    "rejects_m1_false_pass_via_semantic_judge or "
                    "rejects_growth_false_pass_via_semantic_judge or "
                    "accepts_semantic_judge_pass or "
                    "standard_query_processing_verification_failure"
                ),
                "-q",
            ],
        },
        {
            "name": "phase2_staged_truth_state_guards",
            "cmd": [
                str(PYTHON),
                "-m",
                "pytest",
                "backend/tests/test_query_service.py",
                "-k",
                "verification_failure or staged_commit",
                "-q",
            ],
        },
        {
            "name": "phase2_conversation_state_regression",
            "cmd": [
                str(PYTHON),
                "-m",
                "pytest",
                "backend/tests/test_conversation_state_v2.py",
                "-q",
            ],
        },
    ]

    suites = []
    for spec in suite_specs:
        result = run_cmd(spec["cmd"])
        result["name"] = spec["name"]
        suites.append(result)

    summary = {
        "conversation_truth_guarded": all(
            item["returncode"] == 0
            for item in suites
            if item["name"] in {"phase2_staged_truth_state_guards", "phase2_conversation_state_regression"}
        ),
        "named_false_pass_classes_blocked": all(
            item["returncode"] == 0
            for item in suites
            if item["name"] == "phase2_false_pass_and_standard_path"
        ),
        "typed_plan_verification_proven": all(
            item["returncode"] == 0
            for item in suites
            if item["name"] == "phase2_contract_and_typed_plan"
        ),
        "regressions_clear": all(item["returncode"] == 0 for item in suites),
    }

    ok = all(item["returncode"] == 0 for item in suites)
    report = {
        "ok": ok,
        "summary": summary,
        "suites": suites,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_markdown_report(report_path.with_suffix(".md"), report)

    if ok:
        print(f"Phase 2 gate PASS: {report_path}")
        return 0
    print(f"Phase 2 gate FAIL: {report_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
