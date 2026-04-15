from __future__ import annotations

import http.server
import json
import socketserver
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validation" / "probe_direct_batch_viability.py"


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_probe_direct_batch_viability_filters_by_structural_success_and_provider(tmp_path: Path):
    dataset = tmp_path / "direct.jsonl"
    output = tmp_path / "viability.json"
    kept = tmp_path / "kept.jsonl"
    rejected = tmp_path / "rejected.jsonl"

    write_jsonl(
        dataset,
        [
            {
                "id": "direct-1",
                "query": "US GDP",
                "provider_stratum": "FRED",
                "gold": {"accepted_providers": ["FRED"]},
            },
            {
                "id": "direct-2",
                "query": "Japan CPI",
                "provider_stratum": "IMF",
                "gold": {"accepted_providers": ["IMF"]},
            },
            {
                "id": "direct-3",
                "query": "trade balance",
                "provider_stratum": "WorldBank",
                "gold": {"accepted_providers": ["WorldBank"]},
            },
        ],
    )

    responses = {
        "US GDP": {
            "status": 200,
            "body": {
                "data": [
                    {
                        "metadata": {
                            "source": "FRED",
                            "seriesId": "GDP",
                            "country": "US",
                        },
                        "data": [{"date": "2024-01-01", "value": 1}],
                    }
                ]
            },
        },
        "Japan CPI": {
            "status": 200,
            "body": {
                "data": [
                    {
                        "metadata": {
                            "source": "World Bank",
                            "seriesId": "FP.CPI.TOTL",
                            "country": "JP",
                        },
                        "data": [{"date": "2024-01-01", "value": 1}],
                    }
                ]
            },
        },
        "trade balance": {
            "status": 200,
            "body": {
                "clarificationNeeded": True,
                "clarificationQuestions": ["Which country?"],
            },
        },
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            response = responses[payload["query"]]
            body = json.dumps(response["body"]).encode("utf-8")
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    with ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dataset",
                    str(dataset),
                    "--base-url",
                    base_url,
                    "--output",
                    str(output),
                    "--kept-output",
                    str(kept),
                    "--rejected-output",
                    str(rejected),
                ],
                check=True,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["session_count"] == 3
    assert report["summary"]["viability_pass_count"] == 1
    assert report["summary"]["viability_fail_count"] == 2
    assert report["summary"]["reason_counts"]["provider_mismatch"] == 1
    assert report["summary"]["reason_counts"]["clarification_detected"] == 1
    kept_rows = [json.loads(line) for line in kept.read_text(encoding="utf-8").splitlines() if line.strip()]
    rejected_rows = [json.loads(line) for line in rejected.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["id"] for row in kept_rows] == ["direct-1"]
    assert sorted(row["id"] for row in rejected_rows) == ["direct-2", "direct-3"]


def test_probe_direct_batch_viability_respects_max_sessions(tmp_path: Path):
    dataset = tmp_path / "direct.jsonl"
    output = tmp_path / "viability.json"

    write_jsonl(
        dataset,
        [
            {"id": "direct-1", "query": "Q1", "provider_stratum": "FRED", "gold": {"accepted_providers": ["FRED"]}},
            {"id": "direct-2", "query": "Q2", "provider_stratum": "FRED", "gold": {"accepted_providers": ["FRED"]}},
        ],
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.dumps({"data": [{"metadata": {"source": "FRED"}, "data": [{"date": "2024", "value": 1}]}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    with ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dataset",
                    str(dataset),
                    "--base-url",
                    base_url,
                    "--max-sessions",
                    "1",
                    "--output",
                    str(output),
                ],
                check=True,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["session_count"] == 1


def test_probe_direct_batch_viability_retries_transient_timeout(tmp_path: Path):
    dataset = tmp_path / "direct.jsonl"
    output = tmp_path / "viability.json"

    write_jsonl(
        dataset,
        [
            {"id": "direct-1", "query": "retry-me", "provider_stratum": "FRED", "gold": {"accepted_providers": ["FRED"]}},
        ],
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        attempts = 0

        def do_POST(self):  # noqa: N802
            type(self).attempts += 1
            if type(self).attempts == 1:
                import time as _time
                _time.sleep(0.2)
            body = json.dumps({"data": [{"metadata": {"source": "FRED"}, "data": [{"date": "2024", "value": 1}]}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A003
            return

    with ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dataset",
                    str(dataset),
                    "--base-url",
                    base_url,
                    "--timeout-seconds",
                    "0.05",
                    "--max-attempts",
                    "2",
                    "--output",
                    str(output),
                ],
                check=True,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["viability_pass_count"] == 1
    assert report["results"][0]["attempt_count"] == 2


def test_probe_direct_batch_viability_records_timeout_failures_without_crashing(tmp_path: Path):
    dataset = tmp_path / "direct.jsonl"
    output = tmp_path / "viability.json"

    write_jsonl(
        dataset,
        [
            {"id": "direct-1", "query": "always-timeout", "provider_stratum": "FRED", "gold": {"accepted_providers": ["FRED"]}},
        ],
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            import time as _time
            _time.sleep(0.2)

        def log_message(self, format, *args):  # noqa: A003
            return

    with ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dataset",
                    str(dataset),
                    "--base-url",
                    base_url,
                    "--timeout-seconds",
                    "0.05",
                    "--max-attempts",
                    "2",
                    "--output",
                    str(output),
                ],
                check=True,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["viability_pass_count"] == 0
    assert report["summary"]["viability_fail_count"] == 1
    assert report["summary"]["reason_counts"]["exception:ReadTimeout"] == 1
    assert report["results"][0]["attempt_count"] == 2
