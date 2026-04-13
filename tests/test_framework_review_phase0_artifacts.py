from __future__ import annotations

import json
from pathlib import Path

from scripts.release_integrity_check import CHECKS, CANONICAL_DOMAIN, LEGACY_APP_DOMAIN


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_framework_review_plan_artifacts_capture_contract_chain_and_rollout_rules() -> None:
    plan = (REPO_ROOT / ".omx" / "plans" / "plan-framework-review-99-accuracy-consensus.md").read_text(encoding="utf-8")
    prd = (REPO_ROOT / ".omx" / "plans" / "prd-framework-review-99-accuracy.md").read_text(encoding="utf-8")
    test_spec = (REPO_ROOT / ".omx" / "plans" / "test-spec-framework-review-99-accuracy.md").read_text(encoding="utf-8")

    assert "## Authoritative Contract Chain" in plan
    assert "CandidateEvidence" in plan
    assert "VerificationContract" in plan
    assert "false-green gap" in plan
    assert "data.openecon.ai" in plan
    assert "old-path vs new-path truth comparison" in plan

    assert "Canonical production domain is **`data.openecon.ai`**" in prd
    assert "One authoritative semantic-control contract chain." in prd
    assert "Old-path vs new-path truth comparison before rollout promotion." in prd

    assert "Explicit anti-false-green rules" in test_spec
    assert "PASS because any data exists" in test_spec
    assert "Concrete browser/devtools checklist" in test_spec
    assert "data.openecon.ai" in test_spec


def test_restored_design_docs_define_runtime_boundary_and_outcome_system() -> None:
    boundary = (REPO_ROOT / "docs" / "design" / "SEMANTIC_RUNTIME_BOUNDARY.md").read_text(encoding="utf-8")
    system = (REPO_ROOT / "docs" / "design" / "QUERY_OUTCOME_GUARANTEE_SYSTEM.md").read_text(encoding="utf-8")

    assert "Deterministic runtime logic stays structural-only." in boundary
    assert "Equivalent provider substitution is acceptable." in boundary
    assert "verified semantic truth" in boundary.lower()

    assert "SemanticRequest" in system
    assert "ConversationAction" in system
    assert "CandidateEvidence" in system
    assert "VerificationContract" in system
    assert "StateCommitDecision" in system
    assert "browser/manual validation on `https://data.openecon.ai/chat`" in system


def test_release_integrity_checks_cover_live_domain_and_trust_assets() -> None:
    names = {check.name for check in CHECKS}
    live_paths = {check.live_path for check in CHECKS}
    source_paths = {check.source_path for check in CHECKS}
    built_paths = {check.built_path for check in CHECKS}

    assert CANONICAL_DOMAIN == "data.openecon.ai"
    assert LEGACY_APP_DOMAIN == "data.openecon.io"

    assert names == {"chat_html", "robots", "sitemap", "llms", "ai_plugin", "security_txt"}
    assert "packages/frontend/index.html" in source_paths
    assert "packages/frontend/dist/index.html" in built_paths
    assert "packages/frontend/dist/.well-known/ai-plugin.json" in built_paths
    assert "packages/frontend/dist/.well-known/security.txt" in built_paths
    assert "/chat" in live_paths
    assert "/robots.txt" in live_paths
    assert "/sitemap.xml" in live_paths
    assert "/llms.txt" in live_paths
    assert "/.well-known/ai-plugin.json" in live_paths
    assert "/.well-known/security.txt" in live_paths

    for check in CHECKS:
        assert "data.openecon.io" in check.forbidden_substrings
        assert any("data.openecon.ai" in needle for needle in check.required_substrings)
        assert check.structural_evaluator is not None


def test_release_integrity_report_semantics_distinguish_drift_from_source_gaps() -> None:
    report = json.loads((REPO_ROOT / ".omx" / "reports" / "phase0-release-integrity.json").read_text(encoding="utf-8"))
    checks = {item["name"]: item for item in report["checks"]}
    assert "Local comparison target: built frontend artifacts when present" in (
        REPO_ROOT / ".omx" / "reports" / "phase0-release-integrity.md"
    ).read_text(encoding="utf-8")
    assert report["generated_at"].endswith("Z")
    assert set(report["summary"]) == {
        "aligned",
        "deploy_drift",
        "source_and_live_wrong",
        "local_wrong_live_unexpected",
    }

    if report["all_ok"] is True:
        assert sorted(report["summary"]["aligned"]) == sorted(checks)
        assert report["summary"]["deploy_drift"] == []
        assert report["summary"]["source_and_live_wrong"] == []
        assert report["summary"]["local_wrong_live_unexpected"] == []
    else:
        assert report["summary"]["aligned"] == []

    for name in ["chat_html", "robots", "sitemap", "llms", "ai_plugin", "security_txt"]:
        item = checks[name]
        assert item["local_artifact_kind"] == "build"
        assert item["live"]["status_code"] == 200
        assert item["live"]["resolved_url"].startswith("https://data.openecon.ai/")
        assert item["live"]["fetched_at"].endswith("Z")
        assert item["classification"] in {"aligned", "deploy_drift", "source_and_live_wrong", "local_wrong_live_unexpected"}


def test_release_integrity_report_records_structural_problems() -> None:
    report = json.loads((REPO_ROOT / ".omx" / "reports" / "phase0-release-integrity.json").read_text(encoding="utf-8"))
    checks = {item["name"]: item for item in report["checks"]}

    chat = checks["chat_html"]
    assert chat["local"]["structure"]["ok"] is True
    assert chat["live"]["structure"]["ok"] in {True, False}
    if not chat["live"]["structure"]["ok"]:
        assert any("legacy domain" in problem or "non-canonical" in problem for problem in chat["live"]["structure"]["problems"])

    plugin = checks["ai_plugin"]
    assert plugin["local"]["structure"]["ok"] is True
    assert plugin["live"]["structure"]["ok"] in {True, False}
    if not plugin["live"]["structure"]["ok"]:
        assert any("api.url" in problem for problem in plugin["live"]["structure"]["problems"])

    security = checks["security_txt"]
    assert security["local"]["structure"]["ok"] is True
    assert security["live"]["structure"]["ok"] in {True, False}
    if not security["live"]["structure"]["ok"]:
        assert any("Canonical" in problem for problem in security["live"]["structure"]["problems"])


def test_phase0_baseline_truth_is_diagnostic_not_correctness_evidence() -> None:
    baseline_json = json.loads((REPO_ROOT / ".omx" / "reports" / "phase0-baseline-truth.json").read_text(encoding="utf-8"))
    baseline_md = (REPO_ROOT / ".omx" / "reports" / "phase0-baseline-truth.md").read_text(encoding="utf-8")

    assert baseline_json["classification"] == "diagnostic_only"
    assert baseline_json["not_a_correctness_claim"] is True
    assert baseline_json["release_integrity"]["all_ok"] in {True, False}
    assert baseline_json["release_integrity"]["source_report"] == ".omx/reports/phase0-release-integrity.json"
    assert baseline_json["release_integrity"]["generated_at"].endswith("Z")
    assert len(baseline_json["release_integrity"]["source_report_sha256"]) == 64
    assert "diagnostic-only baseline evidence" in baseline_md
    assert "not a correctness proof" in baseline_md
    assert baseline_json["release_integrity"]["generated_at"] in baseline_md
    assert baseline_json["release_integrity"]["source_report_sha256"] in baseline_md
