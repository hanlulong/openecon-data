from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "build_evidence_package.py"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_build_evidence_package_hashes_artifacts_and_surfaces_blockers(tmp_path: Path):
    snapshot = tmp_path / "snapshot.json"
    provider_distribution = tmp_path / "provider.json"
    strata = tmp_path / "strata.json"
    local_score = tmp_path / "local-score.json"
    adjudication = tmp_path / "adjudication.json"
    triage = tmp_path / "triage.json"
    production_score = tmp_path / "production-score.json"
    claim_decision = tmp_path / "claim.json"
    parity_report = tmp_path / "parity.json"
    output = tmp_path / "package.json"

    write_json(snapshot, {"snapshot_date": "2026-04-14", "indicator_count": 330050, "git_sha": "abc123"})
    write_json(provider_distribution, {"snapshot_id": "snap-1", "indicator_count": 330050})
    write_json(strata, {"snapshot_id": "snap-1", "version": 1})
    write_json(local_score, {"snapshot_id": "snap-1", "claim_grade_ready": True, "scoring_mode": "claim_grade", "metrics": {"claim_observed_success": 1.0, "claim_lower95": 0.85}, "snapshot": {"session_count": 22}})
    write_json(adjudication, {"adjudication_complete": True, "completed_records": 22, "total_records": 22})
    write_json(triage, {"summary": {"bucket_counts": {"likely_framework_bug": 1}, "framework_bug_cluster_counts": {"unexpected_clarification_on_direct_query": 1}}})
    write_json(production_score, {"snapshot_id": "snap-1", "claim_grade_ready": False, "scoring_mode": "adjudicated_structural", "claim_grade_blockers": ["prod drift"]})
    write_json(claim_decision, {"claim_allowed": False, "blockers": ["lower95 below threshold"]})
    write_json(parity_report, {"summary": {"sessions_with_differences": 1, "severity_counts": {"material": 1, "match": 21}}})

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--catalog-snapshot",
            str(snapshot),
            "--provider-distribution",
            str(provider_distribution),
            "--strata-definition",
            str(strata),
            "--local-score-report",
            str(local_score),
            "--adjudication-summary",
            str(adjudication),
            "--triage-report",
            str(triage),
            "--production-score-report",
            str(production_score),
            "--claim-decision",
            str(claim_decision),
            "--parity-report",
            str(parity_report),
            "--git-sha",
            "deadbeef",
            "--deployment-timestamp",
            "2026-04-15T01:00:00Z",
            "--deployment-base-url",
            "https://data.openecon.ai",
            "--output",
            str(output),
        ],
        check=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["claim_context"]["git_sha"] == "deadbeef"
    assert report["summary"]["claim_allowed"] is False
    assert report["summary"]["claim_blockers"] == ["lower95 below threshold"]
    assert report["summary"]["package_blockers"] == ["lower95 below threshold"]
    assert report["artifacts"]["claim_decision"]["exists"] is True
    assert len(report["artifacts"]["claim_decision"]["sha256"]) == 64
    assert report["artifacts"]["triage_report"]["summary"]["framework_bug_cluster_counts"] == {"unexpected_clarification_on_direct_query": 1}
    assert report["artifacts"]["parity_report"]["summary"]["sessions_with_differences"] == 1


def test_validation_artifact_schema_lists_evidence_package():
    schema_path = ROOT / "validation" / "schemas" / "evidence_package.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$id"].endswith("/validation/evidence_package.schema.json")
    assert "claim_context" in schema["properties"]
    assert "summary" in schema["properties"]


def test_build_evidence_package_uses_health_timestamp_when_deployment_timestamp_missing(tmp_path: Path):
    snapshot = tmp_path / "snapshot.json"
    provider_distribution = tmp_path / "provider.json"
    strata = tmp_path / "strata.json"
    local_score = tmp_path / "local-score.json"
    adjudication = tmp_path / "adjudication.json"
    triage = tmp_path / "triage.json"
    production_score = tmp_path / "production-score.json"
    claim_decision = tmp_path / "claim.json"
    output = tmp_path / "package.json"

    write_json(snapshot, {"snapshot_date": "2026-04-14", "indicator_count": 330050, "git_sha": "abc123"})
    write_json(provider_distribution, {"snapshot_id": "snap-1", "indicator_count": 330050})
    write_json(strata, {"snapshot_id": "snap-1", "version": 1})
    write_json(local_score, {"snapshot_id": "snap-1", "claim_grade_ready": True, "scoring_mode": "claim_grade", "metrics": {"claim_observed_success": 1.0, "claim_lower95": 0.85}, "snapshot": {"session_count": 22}})
    write_json(adjudication, {"adjudication_complete": True, "completed_records": 22, "total_records": 22})
    write_json(triage, {"summary": {"bucket_counts": {"likely_framework_bug": 0}, "framework_bug_cluster_counts": {}}})
    write_json(production_score, {"snapshot_id": "snap-1", "claim_grade_ready": True, "scoring_mode": "claim_grade"})
    write_json(claim_decision, {"claim_allowed": False, "blockers": ["lower95 below threshold"]})

    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/api/health":
                self.send_response(404)
                self.end_headers()
                return
            payload = {
                "status": "ok",
                "timestamp": "2026-04-15T01:00:00Z",
                "environment": "production",
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    with socketserver.TCPServer(("127.0.0.1", 0), HealthHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--catalog-snapshot",
                    str(snapshot),
                    "--provider-distribution",
                    str(provider_distribution),
                    "--strata-definition",
                    str(strata),
                    "--local-score-report",
                    str(local_score),
                    "--adjudication-summary",
                    str(adjudication),
                    "--triage-report",
                    str(triage),
                    "--production-score-report",
                    str(production_score),
                    "--claim-decision",
                    str(claim_decision),
                    "--git-sha",
                    "deadbeef",
                    "--deployment-base-url",
                    base_url,
                    "--output",
                    str(output),
                ],
                check=True,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["claim_context"]["deployment_timestamp_utc"] == "2026-04-15T01:00:00Z"
    assert "deployment timestamp missing" not in report["summary"]["package_blockers"]
