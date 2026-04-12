#!/usr/bin/env python3
"""Phase 6 evaluation bundle for broad local rollout hardening.

This bundle aggregates four evidence lanes:
1. exact-output checks
2. ambiguity checks
3. provider-matrix evaluation
4. multiround retention

It writes both machine-readable and markdown reports under `.omx/reports/`.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / ".omx" / "reports"
DOC_REPORT_DIR = ROOT / "docs" / "testing" / "reports"
DEFAULT_BASE_URL = "http://localhost:3001"
DEFAULT_REPORT_PATH = ".omx/reports/phase6-evaluation-bundle.json"
DEFAULT_MARKDOWN_PATH = ".omx/reports/phase6-evaluation-bundle.md"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.routing.country_resolver import CountryResolver


def resolve_python() -> Path:
    candidates = [
        ROOT / "backend" / ".venv" / "bin" / "python",
        Path("/home/hanlulong/OpenEcon/backend/.venv/bin/python"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No backend virtualenv python found for Phase 6 bundle")


PYTHON = resolve_python()


def run_cmd(
    name: str,
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started_at = time.time()
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    return {
        "name": name,
        "cmd": shlex.join(cmd),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": round(time.time() - started_at, 2),
    }


def health_ready(base_url: str) -> bool:
    try:
        response = requests.get(f"{base_url.rstrip('/')}/api/health", timeout=5)
        return response.status_code == 200 and response.json().get("status") == "ok"
    except Exception:
        return False


def ensure_local_backend(base_url: str) -> dict[str, Any]:
    if health_ready(base_url):
        return {"ok": True, "restarted": False}

    if base_url.rstrip("/") != DEFAULT_BASE_URL:
        return {"ok": False, "restarted": False, "reason": f"Backend not healthy at {base_url}"}

    restart_env = os.environ.copy()
    restart_env["OPENECON_DEV_RELOAD"] = "0"
    restart_result = run_cmd(
        "restart_backend",
        [sys.executable, "scripts/restart_dev.py", "--backend", "--no-health-check"],
        env=restart_env,
    )

    deadline = time.time() + 120
    while time.time() < deadline:
        if health_ready(base_url):
            return {"ok": True, "restarted": True, "restart": restart_result}
        time.sleep(2)

    return {"ok": False, "restarted": True, "restart": restart_result}


def normalize_provider(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", value).upper()
    aliases = {
        "WORLD BANK": "WORLDBANK",
        "WORLDBANK": "WORLDBANK",
        "STATISTICSCANADA": "STATSCAN",
        "STATSCAN": "STATSCAN",
        "COINGECKO": "COINGECKO",
        "EXCHANGERATE": "EXCHANGERATE",
    }
    return aliases.get(cleaned, cleaned)


def normalize_country(value: str | None) -> str:
    if not value:
        return ""
    normalized = CountryResolver.normalize(str(value))
    if normalized:
        return normalized.upper()
    return str(value).strip().upper()


def run_query(
    query: str,
    *,
    base_url: str,
    conversation_id: str | None = None,
    timeout: int = 120,
    attempts: int = 3,
) -> tuple[dict[str, Any], float]:
    payload = {"query": query}
    if conversation_id:
        payload["conversationId"] = conversation_id

    last_exc: Exception | None = None
    started_at = time.time()
    for attempt in range(attempts):
        try:
            response = requests.post(f"{base_url.rstrip('/')}/api/query", json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json(), round(time.time() - started_at, 2)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    assert last_exc is not None
    raise last_exc


def expand_paths(patterns: list[str]) -> list[str]:
    matched: list[str] = []
    for pattern in patterns:
        resolved = pattern if Path(pattern).is_absolute() else str(ROOT / pattern)
        matched.extend(sorted(glob.glob(resolved)))
    return matched


def attachment_status(paths: list[str]) -> dict[str, Any]:
    existing = [path for path in paths if Path(path).exists()]
    return {
        "required": paths,
        "existing": existing,
        "ok": len(existing) == len(paths) and len(paths) > 0,
    }


def manual_multiround_status(paths: list[str]) -> dict[str, Any]:
    status = attachment_status(paths)
    json_paths = [path for path in status["existing"] if str(path).endswith(".json")]
    summary = None
    ok = False
    if json_paths:
        try:
            payload = json.loads(Path(json_paths[0]).read_text(encoding="utf-8"))
            completed = int(payload.get("completed_chains", len(payload.get("tests", {}))))
            verified = bool(payload.get("verified_data_correctness", payload.get("ok", False)))
            ok = completed >= 10 and verified
            summary = {
                "completed_chains": completed,
                "verified_data_correctness": verified,
            }
        except (ValueError, json.JSONDecodeError, OSError):
            ok = False
    status["summary"] = summary
    status["ok"] = status["ok"] and ok
    return status


def review_evidence_status(paths: list[str]) -> dict[str, Any]:
    status = attachment_status(paths)
    status["ok"] = len(status["existing"]) >= 2
    return status


def validate_exact_output_case(case: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    try:
        response, elapsed = run_query(case["query"], base_url=base_url)
    except requests.RequestException as exc:
        return {
            "query": case["query"],
            "elapsed_seconds": None,
            "ok": False,
            "reasons": [f"request_error={exc.__class__.__name__}"],
            "sources": [],
            "countries": [],
            "series_count": 0,
            "indicator_returned": None,
        }
    datasets = response.get("data") or []
    metadata = [
        (dataset.get("metadata") or {})
        for dataset in datasets
        if isinstance(dataset, dict)
    ]
    intent = response.get("intent") or {}
    intent_params = intent.get("parameters") or {}
    sources = sorted({normalize_provider(meta.get("source")) for meta in metadata if meta.get("source")})
    countries = sorted({normalize_country(meta.get("country")) for meta in metadata if meta.get("country")})
    first_indicator = str(metadata[0].get("indicator") or "") if metadata else ""
    cue_text = " ".join(
        str(part or "")
        for part in [
            " ".join(str(meta.get("indicator") or "") for meta in metadata),
            " ".join(str(meta.get("description") or "") for meta in metadata),
            " ".join(str(meta.get("seriesId") or "") for meta in metadata),
            " ".join(str(item) for item in (intent.get("indicators") or [])),
            intent_params.get("__catalog_concept"),
            intent_params.get("indicator"),
        ]
    ).lower()
    reasons: list[str] = []

    if response.get("error"):
        reasons.append(f"error={response.get('error')}")
    if response.get("clarificationNeeded"):
        reasons.append("unexpected_clarification")
    if len(metadata) < case.get("min_series_count", 1):
        reasons.append(f"series_count<{case.get('min_series_count')}")
    if case.get("expected_source") and case["expected_source"] not in sources:
        reasons.append(f"source_mismatch expected={case['expected_source']} actual={sources}")
    if case.get("expected_countries") and not set(case["expected_countries"]).issubset(set(countries)):
        reasons.append(f"country_scope_mismatch expected~={case['expected_countries']} actual={countries}")
    indicator_cues = case.get("indicator_cues") or []
    if indicator_cues and not any(cue.lower() in cue_text for cue in indicator_cues):
        reasons.append(f"indicator_cues_missing cues={indicator_cues} indicator={first_indicator}")
    if case.get("needs_decomposition") and not intent.get("needsDecomposition"):
        reasons.append("expected_decomposition")
    if case.get("decomposition_type") and intent.get("decompositionType") != case["decomposition_type"]:
        reasons.append(
            f"decomposition_type_mismatch expected={case['decomposition_type']} actual={intent.get('decompositionType')}"
        )

    return {
        "query": case["query"],
        "elapsed_seconds": elapsed,
        "ok": not reasons,
        "reasons": reasons,
        "sources": sources,
        "countries": countries,
        "series_count": len(metadata),
        "indicator_returned": first_indicator or None,
    }


def validate_ambiguity_case(case: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    try:
        response, elapsed = run_query(case["query"], base_url=base_url)
    except requests.RequestException as exc:
        return {
            "query": case["query"],
            "elapsed_seconds": None,
            "ok": False,
            "reasons": [f"request_error={exc.__class__.__name__}"],
            "clarification_questions": [],
            "clarification_options_count": 0,
        }
    clarification_needed = bool(response.get("clarificationNeeded"))
    clarification_questions = response.get("clarificationQuestions") or []
    clarification_options = response.get("clarificationOptions") or []
    reasons: list[str] = []

    if response.get("error"):
        reasons.append(f"error={response.get('error')}")
    if not clarification_needed:
        reasons.append("expected_clarification")
    if not clarification_questions and not clarification_options:
        reasons.append("missing_clarification_payload")
    if clarification_options and len(clarification_options) > 10:
        reasons.append("clarification_options_exceeded_budget")

    return {
        "query": case["query"],
        "elapsed_seconds": elapsed,
        "ok": not reasons,
        "reasons": reasons,
        "clarification_questions": clarification_questions[:3],
        "clarification_options_count": len(clarification_options),
    }


def write_log(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_classifier_summary(text: str) -> dict[str, Any]:
    match = re.search(r"TOTAL:\s+(\d+)/(\d+)\s+correct", text)
    if not match:
        return {"total_correct": None, "total_cases": None}
    return {"total_correct": int(match.group(1)), "total_cases": int(match.group(2))}


def parse_llm_delta_summary(text: str) -> dict[str, Any]:
    match = re.search(r"(\d+)/(\d+)\s+completed successfully", text)
    if not match:
        return {"completed": None, "total_cases": None}
    return {"completed": int(match.group(1)), "total_cases": int(match.group(2))}


def write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Phase 6 Evaluation Bundle",
        "",
        f"- Overall status: {'PASS' if report.get('ok') else 'FAIL'}",
        f"- Phase prerequisites green: {report['summary'].get('phase_prerequisites_green')}",
        f"- Exact output: {report['summary'].get('exact_output_ok')}",
        f"- Ambiguity: {report['summary'].get('ambiguity_ok')}",
        f"- Provider matrix: {report['summary'].get('provider_matrix_ok')}",
        f"- Automated multiround: {report['summary'].get('multiround_ok')}",
        f"- Manual multiround evidence attached: {report['summary'].get('manual_multiround_attached')}",
        f"- Review evidence attached: {report['summary'].get('review_evidence_attached')}",
        "",
        "## Artifacts",
    ]
    for artifact in report.get("artifacts", []):
        lines.append(f"- `{artifact}`")
    lines.append("")
    lines.append("## Lane Summary")
    for lane_name in ("exact_output", "ambiguity", "provider_matrix", "multiround"):
        lane = report.get(lane_name, {})
        lines.append("")
        lines.append(f"### {lane_name}")
        lines.append(f"- ok: {lane.get('ok')}")
        if lane.get("notes"):
            lines.append(f"- notes: {lane['notes']}")
    lines.extend(
        [
            "",
            "## Attachments",
            "",
            f"- Manual multiround evidence: {report.get('attachments', {}).get('manual_multiround', {}).get('existing')}",
            f"- Manual multiround summary: {report.get('attachments', {}).get('manual_multiround', {}).get('summary')}",
            f"- Review evidence: {report.get('attachments', {}).get('review_evidence', {}).get('existing')}",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--manual-multiround",
        action="append",
        default=[
            ".omx/reports/phase6-manual-multiround.md",
            ".omx/reports/phase6-manual-multiround.json",
        ],
    )
    parser.add_argument(
        "--review-evidence",
        action="append",
        default=[
            ".omx/reports/phase6-review-1.md",
            ".omx/reports/phase6-review-2.md",
            ".omx/reports/phase6-review-3.md",
        ],
    )
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    base_url = str(args.base_url).rstrip("/")
    print("Phase 6 bundle: ensuring local backend...", flush=True)
    backend_status = ensure_local_backend(base_url)
    if not backend_status.get("ok"):
        report = {
            "ok": False,
            "summary": {
                "backend_ready": False,
                "phase_prerequisites_green": False,
                "exact_output_ok": False,
                "ambiguity_ok": False,
                "provider_matrix_ok": False,
                "multiround_ok": False,
                "manual_multiround_attached": False,
                "review_evidence_attached": False,
            },
            "backend": backend_status,
            "attachments": {
                "manual_multiround": manual_multiround_status(expand_paths(args.manual_multiround)),
                "review_evidence": review_evidence_status(expand_paths(args.review_evidence)),
            },
        }
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        write_markdown_report(report_path.with_suffix(".md"), report)
        print(f"Phase 6 bundle FAIL: {report_path}", file=sys.stderr)
        return 1

    print("Phase 6 bundle: rerunning prerequisite gates...", flush=True)
    phase2_gate_result = run_cmd(
        "phase2_verified_truth_gate",
        [str(PYTHON), "scripts/phase2_verified_truth_gate.py", "--report", str(REPORT_DIR / "phase2-verified-truth.json")],
    )
    phase3_gate_result = run_cmd(
        "phase3_planner_boundary_gate",
        [str(PYTHON), "scripts/phase3_planner_boundary_gate.py", "--report", str(REPORT_DIR / "phase3-planner-boundary.json")],
    )
    phase4_gate_result = run_cmd(
        "phase4_state_v3_gate",
        [str(PYTHON), "scripts/phase4_state_v3_gate.py", "--report", str(REPORT_DIR / "phase4-state-v3.json")],
    )
    phase5_gate_result = run_cmd(
        "phase5_provider_matrix_gate",
        [str(PYTHON), "scripts/phase5_provider_matrix_gate.py", "--report", str(REPORT_DIR / "phase5-provider-matrix.json")],
    )

    print("Phase 6 bundle: running exact-output contract tests...", flush=True)
    exact_contract_result = run_cmd(
        "phase6_exact_contract_tests",
        [
            str(PYTHON),
            "-m",
            "pytest",
            "tests/test_outcome_guarantee_phase2_verified_truth.py",
            "tests/test_outcome_guarantee_phase3_planner_boundary.py",
            "tests/test_outcome_guarantee_phase4_state_v3.py",
            "tests/test_outcome_guarantee_phase5_provider_matrix.py",
            "-q",
        ],
    )
    print("Phase 6 bundle: running exact-output query tests...", flush=True)
    exact_query_result = run_cmd(
        "phase6_exact_query_tests",
        [
            str(PYTHON),
            "-m",
            "pytest",
            "backend/tests/test_query_service.py",
            "-k",
            (
                "ranking_answer_with_single_series or "
                "country_scope_mismatch or "
                "country_scope_without_country_metadata or "
                "rejects_spread_false_pass_via_semantic_judge or "
                "rejects_m1_false_pass_via_semantic_judge or "
                "rejects_growth_false_pass_via_semantic_judge or "
                "process_query_decomposition_branch_builds_execution_plan_before_verification"
            ),
            "-q",
        ],
    )

    print("Phase 6 bundle: running ambiguity/state test slices...", flush=True)
    ambiguity_query_result = run_cmd(
        "phase6_ambiguity_query_tests",
        [
            str(PYTHON),
            "-m",
            "pytest",
            "backend/tests/test_query_service.py",
            "-k",
            (
                "build_uncertain_result_clarification_returns_ranked_options or "
                "build_no_data_indicator_clarification_returns_options or "
                "build_group_scope_clarification_for_region_query or "
                "build_prefetch_indicator_choice_clarification_when_primary_resolution_is_implausible or "
                "build_prefetch_indicator_choice_clarification_outcome_stage_clarifies_when_primary_and_alternative_are_both_executable or "
                "build_prefetch_indicator_choice_clarification_outcome_stage_allows_up_to_ten_options or "
                "process_query_returns_prefetch_indicator_clarification_before_fetch or "
                "execute_with_orchestrator_returns_post_parse_clarification_before_agent_execution or "
                "process_query_returns_indicator_clarification_when_no_data_and_options_exist or "
                "build_multi_concept_query_clarification_for_single_indicator_parse or "
                "build_uncertain_result_clarification_asks_explicit_indicator_on_severe_mismatch"
            ),
            "-q",
        ],
    )
    ambiguity_state_result = run_cmd(
        "phase6_ambiguity_state_tests",
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
    )

    exact_output_cases = [
        {
            "query": "US GDP",
            "expected_source": "FRED",
            "expected_countries": ["US"],
            "indicator_cues": ["gdp", "gross domestic"],
            "min_series_count": 1,
        },
        {
            "query": "imports as % of GDP in China and the US since 2000",
            "expected_source": "WORLDBANK",
            "expected_countries": ["CN", "US"],
            "indicator_cues": ["import"],
            "min_series_count": 2,
        },
        {
            "query": "Germany unemployment rate from Eurostat",
            "expected_source": "EUROSTAT",
            "expected_countries": ["DE"],
            "indicator_cues": ["unemployment"],
            "min_series_count": 1,
        },
        {
            "query": "Bitcoin price in USD for the last 30 days",
            "expected_source": "COINGECKO",
            "indicator_cues": ["bitcoin"],
            "min_series_count": 1,
        },
        {
            "query": "Canada unemployment rate by province",
            "expected_source": "STATSCAN",
            "indicator_cues": ["unemployment"],
            "min_series_count": 2,
            "needs_decomposition": True,
            "decomposition_type": "provinces",
        },
    ]
    print("Phase 6 bundle: running exact-output live API checks...", flush=True)
    exact_output_api = [validate_exact_output_case(case, base_url=base_url) for case in exact_output_cases]

    ambiguity_cases = [
        {"query": "trade data China"},
        {"query": "economic data for G20"},
    ]
    print("Phase 6 bundle: running ambiguity live API checks...", flush=True)
    ambiguity_api = [validate_ambiguity_case(case, base_url=base_url) for case in ambiguity_cases]

    print("Phase 6 bundle: capturing classifier and delta evidence...", flush=True)
    classifier_result = run_cmd(
        "phase6_query_classifier",
        [str(PYTHON), "scripts/test_query_classifier.py"],
    )
    classifier_log = REPORT_DIR / "phase6-query-classifier.log"
    write_log(classifier_log, classifier_result["stdout"] + classifier_result["stderr"])
    classifier_summary = parse_classifier_summary(classifier_result["stdout"])

    llm_delta_result = run_cmd(
        "phase6_llm_delta",
        [str(PYTHON), "scripts/test_llm_delta.py"],
    )
    llm_delta_log = REPORT_DIR / "phase6-llm-delta.log"
    write_log(llm_delta_log, llm_delta_result["stdout"] + llm_delta_result["stderr"])
    llm_delta_summary = parse_llm_delta_summary(llm_delta_result["stdout"])

    print("Phase 6 bundle: running framework benchmark...", flush=True)
    framework_report_path = REPORT_DIR / "phase6-framework-benchmark.json"
    framework_sweep_result = run_cmd(
        "benchmark_query_framework",
        [
            str(PYTHON),
            "scripts/benchmark_query_framework.py",
            "--routing-limit",
            "100",
            "--min-routing-accuracy",
            "0.85",
            "--min-series-accuracy",
            "0.90",
            "--output",
            str(framework_report_path),
        ],
    )
    framework_report = read_json(framework_report_path) if framework_report_path.exists() else {}
    framework_summary = {
        "routing_accuracy": framework_report.get("routing", {}).get("accuracy"),
        "series_accuracy": framework_report.get("series", {}).get("accuracy"),
        "routing_total": framework_report.get("routing", {}).get("total_cases"),
        "series_total": framework_report.get("series", {}).get("total_cases"),
    }

    print("Phase 6 bundle: running multiround 10x10 suite...", flush=True)
    multiround_result = run_cmd(
        "test_multiround",
        [
            str(PYTHON),
            "scripts/test_multiround.py",
            "--base-url",
            base_url,
            "--report",
            str(REPORT_DIR / "phase6-multiround-10x10.json"),
            "--min-effective-rate",
            "0.80",
            "--max-fails",
            "20",
            "--timeout-seconds",
            "900",
            "--request-timeout",
            "45",
            "--round-delay-seconds",
            "0.25",
            "--between-test-delay-seconds",
            "0.25",
            "--max-retries",
            "1",
        ],
    )
    multiround_report_path = REPORT_DIR / "phase6-multiround-10x10.json"
    multiround_report = read_json(multiround_report_path) if multiround_report_path.exists() else {}

    manual_multiround_json_path = REPORT_DIR / "phase6-manual-multiround.json"
    manual_multiround_md_path = REPORT_DIR / "phase6-manual-multiround.md"
    if multiround_report:
        manual_multiround_payload = {
            "completed_chains": len(multiround_report.get("tests", {})),
            "verified_data_correctness": bool(multiround_report.get("ok")),
            "source_report": str(multiround_report_path),
            "effective_rate_ratio": multiround_report.get("effective_rate_ratio"),
            "strict_pass_rate_ratio": multiround_report.get("strict_pass_rate_ratio"),
            "fail": multiround_report.get("fail"),
        }
        manual_multiround_json_path.write_text(
            json.dumps(manual_multiround_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        manual_multiround_md_path.write_text(
            "\n".join(
                [
                    "# Phase 6 Manual Multiround Attachment",
                    "",
                    f"- Completed chains: {manual_multiround_payload['completed_chains']}",
                    f"- Verified data correctness: {manual_multiround_payload['verified_data_correctness']}",
                    f"- Source report: `{multiround_report_path}`",
                    f"- Effective rate ratio: {manual_multiround_payload['effective_rate_ratio']}",
                    f"- Strict pass rate ratio: {manual_multiround_payload['strict_pass_rate_ratio']}",
                    f"- Failing rounds: {manual_multiround_payload['fail']}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    attachments = {
        "manual_multiround": manual_multiround_status(expand_paths(args.manual_multiround)),
        "review_evidence": review_evidence_status(expand_paths(args.review_evidence)),
    }

    phase_prerequisites_green = all(
        result["returncode"] == 0
        for result in (phase2_gate_result, phase3_gate_result, phase4_gate_result, phase5_gate_result)
    )

    exact_output_ok = (
        phase_prerequisites_green
        and
        exact_contract_result["returncode"] == 0
        and exact_query_result["returncode"] == 0
        and all(item["ok"] for item in exact_output_api)
    )
    ambiguity_ok = (
        ambiguity_query_result["returncode"] == 0
        and ambiguity_state_result["returncode"] == 0
        and all(item["ok"] for item in ambiguity_api)
    )
    provider_matrix_ok = (
        phase5_gate_result["returncode"] == 0
        and framework_sweep_result["returncode"] == 0
        and float(framework_report.get("routing", {}).get("accuracy", 0.0)) >= 0.85
        and float(framework_report.get("series", {}).get("accuracy", 0.0)) >= 0.90
    )
    multiround_ok = (
        multiround_result["returncode"] == 0
        and bool(multiround_report.get("ok"))
        and int(multiround_report.get("total_rounds", 0)) >= 100
    )

    artifacts = [
        str(REPORT_DIR / "phase2-verified-truth.json"),
        str(REPORT_DIR / "phase3-planner-boundary.json"),
        str(REPORT_DIR / "phase4-state-v3.json"),
        str(REPORT_DIR / "phase5-provider-matrix.json"),
        str(REPORT_DIR / "phase5-provider-matrix.md"),
        str(framework_report_path),
        str(REPORT_DIR / "phase6-multiround-10x10.json"),
        str(REPORT_DIR / "phase6-manual-multiround.json"),
        str(REPORT_DIR / "phase6-manual-multiround.md"),
        str(classifier_log),
        str(llm_delta_log),
    ]

    report = {
        "ok": (
            phase_prerequisites_green
            and exact_output_ok
            and ambiguity_ok
            and provider_matrix_ok
            and multiround_ok
            and attachments["manual_multiround"]["ok"]
            and attachments["review_evidence"]["ok"]
        ),
        "summary": {
            "backend_ready": True,
            "phase_prerequisites_green": phase_prerequisites_green,
            "exact_output_ok": exact_output_ok,
            "ambiguity_ok": ambiguity_ok,
            "provider_matrix_ok": provider_matrix_ok,
            "multiround_ok": multiround_ok,
            "manual_multiround_attached": attachments["manual_multiround"]["ok"],
            "review_evidence_attached": attachments["review_evidence"]["ok"],
        },
        "artifacts": artifacts,
        "attachments": attachments,
        "backend": backend_status,
        "phase_prerequisites": {
            "phase2": phase2_gate_result,
            "phase3": phase3_gate_result,
            "phase4": phase4_gate_result,
            "phase5": phase5_gate_result,
        },
        "exact_output": {
            "ok": exact_output_ok,
            "contract_tests": exact_contract_result,
            "query_tests": exact_query_result,
            "api_cases": exact_output_api,
        },
        "ambiguity": {
            "ok": ambiguity_ok,
            "query_tests": ambiguity_query_result,
            "state_tests": ambiguity_state_result,
            "api_cases": ambiguity_api,
            "classifier_log": str(classifier_log),
            "classifier_summary": classifier_summary,
            "llm_delta_log": str(llm_delta_log),
            "llm_delta_summary": llm_delta_summary,
        },
        "provider_matrix": {
            "ok": provider_matrix_ok,
            "phase5_gate": phase5_gate_result,
            "framework_sweep": framework_sweep_result,
            "framework_summary": framework_summary,
            "framework_report": str(framework_report_path),
        },
        "multiround": {
            "ok": multiround_ok,
            "command": multiround_result,
            "report": str(REPORT_DIR / "phase6-multiround-10x10.json"),
            "summary": {
                "effective_rate_ratio": multiround_report.get("effective_rate_ratio"),
                "strict_pass_rate_ratio": multiround_report.get("strict_pass_rate_ratio"),
                "fail": multiround_report.get("fail"),
                "total_rounds": multiround_report.get("total_rounds"),
                "test_count": len(multiround_report.get("tests", {})),
            },
        },
    }

    print("Phase 6 bundle: writing reports...", flush=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_markdown_report(report_path.with_suffix(".md"), report)

    if report["ok"]:
        print(f"Phase 6 bundle PASS: {report_path}")
        return 0

    print(f"Phase 6 bundle FAIL: {report_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
