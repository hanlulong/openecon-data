"""Monitor regressions without public requests, paid tools, or service changes."""

import copy
import datetime
import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest.mock import patch

import pytest


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


websites = load_module("website_reliability_probe", "check_websites.py")
with patch.dict(sys.modules, {"check_websites": websites}):
    monitor = load_module("website_reliability_monitor", "monitor.py")


def website_report(degraded=False):
    return {"results": [{
        "host": host, "scheme": scheme, "ip_family": family,
        "http_status": 503 if degraded and host in websites.KNOWN_DOWN else 200,
        "curl_exit_code": 0, "tls_verified": True,
        "canonical_target_verified": True,
    } for host, scheme, family in sorted(websites.JOBS)]}


def complete_saved_report(age=0, degraded=False):
    return {
        "schema_version": 1,
        "checked_at": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age)).isoformat(),
        "status": "DEGRADED" if degraded else "HEALTHY",
        "failed_checks": [],
        "known_degraded": sorted(websites.KNOWN_DOWN) if degraded else [],
        "checks": {
            "websites": monitor.website_summary(website_report(degraded=degraded)),
            "mcp": {"ok": True, "reason": "verified", "timed_out": False},
            "backends": {"ok": True, "checks": [{"port": port, "ok": True} for port in (3001, 3002)]},
        },
    }


def test_inventory_covers_exactly_twenty_hosts_two_schemes_and_two_ip_families():
    assert len(websites.HOSTS) == 20
    assert len(websites.JOBS) == 80
    assert websites.JOBS == {(host, scheme, family) for host in websites.HOSTS for scheme in ("http", "https") for family in (4, 6)}
    assert monitor.website_summary(website_report())["ok"] is True


@pytest.mark.parametrize("mutate", [
    lambda report: report["results"].pop(),
    lambda report: report["results"].append(report["results"][0]),
    lambda report: report["results"].__setitem__(0, copy.deepcopy(report["results"][1])),
    lambda report: report["results"][0].__setitem__("host", "unexpected.example"),
])
def test_incomplete_duplicate_and_wrong_website_inventory_cannot_pass(mutate):
    report = website_report()
    mutate(report)
    with pytest.raises((ValueError, TypeError, KeyError)):
        monitor.website_summary(report)


@pytest.mark.parametrize("contents", ["not json", "null", "[]", "{}", '{"results":null}', '{"results":[{}]}'])
def test_malformed_child_report_fails_closed(tmp_path, monkeypatch, contents):
    (tmp_path / "websites.json").write_text(contents)
    monkeypatch.setattr(monitor, "run_probe", lambda *args: {"exit_code": 0, "output": "", "timed_out": False})
    try:
        result = monitor.check_websites(tmp_path)
    except (ValueError, TypeError, AttributeError, KeyError):
        pytest.fail("Malformed child output should become an explicit failed check")
    assert result["ok"] is False


def test_known_503s_are_visible_degradation_and_recovery_to_200_is_healthy():
    degraded = monitor.website_summary(website_report(degraded=True))
    assert degraded["ok"] is True
    assert degraded["known_degraded"] == sorted(websites.KNOWN_DOWN)
    recovered = monitor.website_summary(website_report())
    assert recovered["ok"] is True
    assert recovered["known_degraded"] == []


@pytest.mark.parametrize("field,value", [
    ("tls_verified", False), ("canonical_target_verified", False),
    ("curl_exit_code", 28), ("http_status", 500),
])
def test_known_down_label_never_masks_transport_tls_target_or_other_status_failure(field, value):
    report = website_report(degraded=True)
    row = next(row for row in report["results"] if row["host"] in websites.KNOWN_DOWN)
    row[field] = value
    summary = monitor.website_summary(report)
    assert summary["ok"] is False
    assert len(summary["failed_checks"]) == 1


def test_new_503_is_a_regression():
    report = website_report()
    next(row for row in report["results"] if row["host"] == "luhan.io")["http_status"] = 503
    assert monitor.website_summary(report)["ok"] is False


@pytest.mark.parametrize("final_url,verified,expected", [
    ("https://openecon.ai/", 0, True),
    ("https://unexpected.example/", 0, False),
    ("http://openecon.ai/", 0, False),
    ("https://openecon.ai:8444/", 0, False),
    ("https://openecon.ai/", 60, False),
])
def test_curl_probe_enforces_tls_and_exact_canonical_target(monkeypatch, final_url, verified, expected):
    def curl(arguments, **kwargs):
        assert arguments[:4] == ["curl", "-q", "--noproxy", "*"]
        assert kwargs["timeout"] <= 15
        return subprocess.CompletedProcess(arguments, 0, json.dumps({
            "http_code": 200, "ssl_verify_result": verified, "url_effective": final_url,
        }), "")

    monkeypatch.setattr(websites.subprocess, "run", curl)
    assert websites.probe(("deepecon.ai", "http", 6))["ok"] is expected


def test_timeout_or_child_failure_cannot_reuse_a_good_website_report(tmp_path, monkeypatch):
    (tmp_path / "websites.json").write_text(json.dumps(website_report()))
    for result in (
        {"exit_code": None, "output": "", "timed_out": True},
        {"exit_code": 1, "output": "", "timed_out": False},
        {"exit_code": 2, "output": "", "timed_out": False},
    ):
        monkeypatch.setattr(monitor, "run_probe", lambda *args, result=result: result)
        assert monitor.check_websites(tmp_path)["ok"] is False


@pytest.mark.parametrize("payload,status,expected", [
    (b'{"status":"ok"}', 200, True),
    (b'{"status":"error"}', 200, False),
    (b'{"status":"ok"}', 503, False),
    (b"null", 200, False),
    (b"invalid json", 200, False),
])
def test_backend_health_requires_http_200_and_status_ok(monkeypatch, payload, status, expected):
    class Response:
        def __enter__(self):
            self.status = status
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit <= 1024 * 1024
            return payload

    class Opener:
        def open(self, url, timeout):
            assert url in ("http://127.0.0.1:3001/api/health", "http://127.0.0.1:3002/api/health")
            assert timeout <= 5
            return Response()

    monkeypatch.setattr(monitor.urllib.request, "build_opener", lambda *args: Opener())
    result = monitor.check_backends()
    assert result["ok"] is expected
    assert {check["port"] for check in result["checks"]} == {3001, 3002}


def test_backend_read_timeout_is_an_explicit_failed_check(monkeypatch):
    class Opener:
        def open(self, url, timeout):
            raise TimeoutError("isolated timeout")

    monkeypatch.setattr(monitor.urllib.request, "build_opener", lambda *args: Opener())
    result = monitor.check_backends()
    assert result["ok"] is False
    assert all(check["error_type"] == "TimeoutError" for check in result["checks"])


@pytest.mark.parametrize("missing", ["initialized", "tools_listed", "query_data_available", "session_closed"])
def test_mcp_probe_requires_every_handshake_and_cleanup_condition(monkeypatch, missing):
    body = {"ok": True, "head_status": 405, "initialized": True, "tools_listed": True, "query_data_available": True, "session_closed": True}
    body[missing] = False
    monkeypatch.setattr(monitor, "run_probe", lambda *args: {"exit_code": 0, "output": json.dumps(body), "timed_out": False})
    assert monitor.check_mcp()["ok"] is False


@pytest.mark.parametrize("degraded", [False, True])
def test_recent_consistent_saved_status_is_readable(tmp_path, degraded):
    target = tmp_path / "latest.json"
    target.write_text(json.dumps(complete_saved_report(degraded=degraded)))
    assert monitor.saved_status(target)["status"] == ("DEGRADED" if degraded else "HEALTHY")


@pytest.mark.parametrize("mutate", [
    lambda report: report.update(checked_at=complete_saved_report(age=601)["checked_at"]),
    lambda report: report.update(checked_at=complete_saved_report(age=-120)["checked_at"]),
    lambda report: report.update(status="RUNNING"),
    lambda report: report.update(status="FAILED"),
    lambda report: report.pop("checked_at"),
    lambda report: report.update(failed_checks=["backends"]),
    lambda report: report.pop("checks"),
    lambda report: report["checks"].pop("mcp"),
    lambda report: report["checks"]["backends"].update(ok=False),
    lambda report: report.update(known_degraded=["app.hansearch.com"]),
    lambda report: report.update(status="DEGRADED"),
    lambda report: report["checks"]["websites"]["results"].pop(),
    lambda report: report["checks"]["websites"]["results"][0].update(tls_verified=False),
    lambda report: report["checks"]["backends"]["checks"].pop(),
    lambda report: report["checks"]["backends"]["checks"][0].update(ok=False),
    lambda report: report["checks"]["mcp"].update(timed_out=True),
])
def test_stale_incomplete_or_contradictory_saved_status_cannot_certify_health(tmp_path, mutate):
    target = tmp_path / "latest.json"
    report = complete_saved_report()
    mutate(report)
    target.write_text(json.dumps(report))
    assert monitor.saved_status(target)["status"] == "FAILED"


def test_atomic_state_replacement_is_complete_private_and_preserves_previous_on_error(tmp_path, monkeypatch):
    target = tmp_path / "latest.json"
    previous = {"status": "HEALTHY"}
    target.write_text(json.dumps(previous))
    original_replace = monitor.os.replace
    replacements = []

    def verify_replace(source, destination):
        replacements.append(json.loads(Path(source).read_text()))
        assert json.loads(target.read_text()) == previous
        original_replace(source, destination)

    monkeypatch.setattr(monitor.os, "replace", verify_replace)
    monitor.atomic_json(target, {"status": "FAILED"})
    assert replacements == [{"status": "FAILED"}]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(TypeError):
        monitor.atomic_json(target, {"invalid": object()})
    assert json.loads(target.read_text()) == {"status": "FAILED"}
    assert list(tmp_path.glob(".monitor-*")) == []


@pytest.mark.parametrize("failed,degraded,status,exit_code", [
    (False, False, "HEALTHY", 0), (False, True, "DEGRADED", 0),
    (True, True, "FAILED", 1),
])
def test_main_marks_running_then_aggregates_all_checks(tmp_path, monkeypatch, failed, degraded, status, exit_code):
    target = tmp_path / "latest.json"
    states = []
    original_write = monitor.atomic_json

    def capture(path, value):
        states.append(value["status"])
        original_write(path, value)

    monkeypatch.setattr(monitor, "atomic_json", capture)
    monkeypatch.setattr(monitor, "check_websites", lambda *args: {"ok": True, "known_degraded": sorted(websites.KNOWN_DOWN) if degraded else []})
    monkeypatch.setattr(monitor, "check_mcp", lambda: {"ok": True})
    monkeypatch.setattr(monitor, "check_backends", lambda: {"ok": not failed})
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--output", str(target)])
    assert monitor.main() == exit_code
    assert states == ["RUNNING", status]
    report = json.loads(target.read_text())
    assert report["status"] == status
    assert report["failed_checks"] == (["backends"] if failed else [])
    assert set(report["checks"]) == {"websites", "mcp", "backends"}
    assert list(tmp_path.glob("probe-*")) == []


def test_timeout_kills_only_the_probe_process_group_including_its_child(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = monitor.run_probe([sys.executable, str(script), str(child_pid_file)], timeout=0.8)
    assert result["timed_out"] is True
    assert time.monotonic() - started < 4
    child_pid = int(child_pid_file.read_text())
    process_status = Path(f"/proc/{child_pid}/status")
    deadline = time.monotonic() + 1
    while process_status.exists() and time.monotonic() < deadline:
        try:
            status_lines = process_status.read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            break  # The process exited between the existence check and read.
        if any(line.startswith("State:") and ("Z" in line or "X" in line) for line in status_lines):
            break  # A terminated orphan may await reaping by the host's PID1.
        time.sleep(0.01)
    else:
        assert not process_status.exists(), "Probe descendant survived process-group timeout cleanup"


def failed_mcp_report():
    report = complete_saved_report(degraded=True)
    report.update(status="FAILED", failed_checks=["mcp"])
    report["checks"]["mcp"] = {
        "ok": False, "timed_out": False,
        "reason": "mcp_handshake_or_discovery_failed",
        "diagnostic": {"code": "capacity_rejected", "stage": "admission",
                       "method": "GET", "http_status": 503, "retry_after_seconds": 30},
    }
    return report


def test_fresh_complete_failed_status_reports_real_check_and_safe_diagnostic(tmp_path):
    target = tmp_path / "latest.json"
    report = failed_mcp_report()
    report["checks"]["mcp"]["diagnostic"]["url"] = "https://example/mcp?session_id=SECRET"
    report["checks"]["mcp"]["response_body"] = "SECRET"
    target.write_text(json.dumps(report))
    result = monitor.saved_status(target)
    assert result["status"] == "FAILED"
    assert result["reason"] == "checks_failed"
    assert result["failed_checks"] == ["mcp"]
    assert result["checked_at"] == report["checked_at"]
    assert result["failure_details"]["mcp"]["diagnostic"] == {
        "code": "capacity_rejected", "stage": "admission", "method": "GET",
        "http_status": 503, "retry_after_seconds": 30,
    }
    assert "SECRET" not in json.dumps(result)


def test_status_cli_keeps_nonzero_exit_for_fresh_capacity_failure(tmp_path, monkeypatch, capsys):
    target = tmp_path / "latest.json"
    target.write_text(json.dumps(failed_mcp_report()))
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--status", "--output", str(target)])
    assert monitor.main() == 1
    assert json.loads(capsys.readouterr().out)["failed_checks"] == ["mcp"]


@pytest.mark.parametrize("kind,expected", [
    ("missing", "missing_report"), ("malformed", "invalid_report"),
    ("stale", "stale_report"), ("running", "incomplete_report"),
])
def test_unavailable_saved_states_have_distinct_safe_reasons(tmp_path, kind, expected):
    target = tmp_path / "latest.json"
    if kind == "malformed":
        target.write_text("SECRET not json")
    elif kind == "stale":
        target.write_text(json.dumps(complete_saved_report(age=601)))
    elif kind == "running":
        target.write_text(json.dumps({"schema_version": 1, "status": "RUNNING", "checks": {}}))
    result = monitor.saved_status(target)
    assert result == {"status": "FAILED", "reason": expected}


@pytest.mark.parametrize("mutate", [
    lambda report: report.update(failed_checks=[]),
    lambda report: report.update(failed_checks=["websites"]),
    lambda report: report.update(failed_checks=["mcp", "mcp"]),
    lambda report: report["checks"]["mcp"].update(ok=True),
    lambda report: report["checks"].pop("backends"),
    lambda report: report["checks"]["websites"]["results"].pop(),
    lambda report: report["checks"]["backends"]["checks"].pop(),
    lambda report: report.update(known_degraded=["unexpected.example"]),
])
def test_failed_report_does_not_bypass_consistency_or_positive_checks(tmp_path, mutate):
    target = tmp_path / "latest.json"
    report = failed_mcp_report()
    mutate(report)
    target.write_text(json.dumps(report))
    assert monitor.saved_status(target) == {"status": "FAILED", "reason": "invalid_report"}


def test_mcp_child_capacity_failure_is_retained_with_only_allowed_fields(monkeypatch):
    body = {"ok": False, "diagnostic": {
        "code": "capacity_rejected", "stage": "admission", "method": "GET",
        "http_status": 503, "retry_after_seconds": 30,
        "message": "SECRET", "url": "https://example/?session_id=SECRET",
    }}
    monkeypatch.setattr(monitor, "run_probe", lambda *args: {
        "exit_code": 1, "output": json.dumps(body), "timed_out": False,
    })
    result = monitor.check_mcp()
    assert result["ok"] is False
    assert result["diagnostic"]["http_status"] == 503
    assert result["diagnostic"]["code"] == "capacity_rejected"
    assert "SECRET" not in json.dumps(result)


def test_untrusted_mcp_child_diagnostic_values_are_not_echoed(monkeypatch):
    body = {"ok": False, "diagnostic": {
        "code": "SECRET", "stage": "SECRET", "method": "SECRET",
        "http_status": "SECRET", "retry_after_seconds": "SECRET", "error_type": "SECRET",
    }}
    monkeypatch.setattr(monitor, "run_probe", lambda *args: {
        "exit_code": 1, "output": json.dumps(body), "timed_out": False,
    })
    result = monitor.check_mcp()
    assert result["ok"] is False
    assert result["diagnostic"] == {"code": "probe_failed"}
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("retry,expected", [("30", 30), (" 30 ", 30), ("-1", None), ("SECRET", None), ("999999999", None)])
def test_nested_sdk_http_errors_report_capacity_without_sensitive_values(retry, expected):
    import httpx
    sdk_probe = load_module("website_reliability_sdk_probe", "check_mcp_session.py")
    request = httpx.Request("GET", "https://data.openecon.ai/mcp?session_id=SECRET", headers={"Authorization": "SECRET"})
    response = httpx.Response(503, headers={"Retry-After": retry}, content=b"SECRET", request=request)
    error = ExceptionGroup("SECRET", [ExceptionGroup("SECRET", [
        httpx.HTTPStatusError("SECRET", request=request, response=response),
    ])])
    result = sdk_probe.failure_diagnostic(error)
    assert result["code"] == "capacity_rejected"
    assert result["stage"] == "admission"
    assert result["method"] == "GET"
    assert result["http_status"] == 503
    assert result.get("retry_after_seconds") == expected
    assert "SECRET" not in json.dumps(result)


def test_unexpected_sdk_exception_text_is_never_used_as_diagnostic():
    sdk_probe = load_module("website_reliability_sdk_probe", "check_mcp_session.py")
    result = sdk_probe.failure_diagnostic(ExceptionGroup("SECRET", [ValueError("SECRET")]))
    assert result == {"code": "unexpected_error", "stage": "session"}


def test_sdk_cli_serializes_nested_failure_without_exception_text(monkeypatch, capsys):
    sdk_probe = load_module("website_reliability_sdk_probe", "check_mcp_session.py")

    def fail(*args):
        raise ExceptionGroup("SECRET", [TimeoutError("SECRET")])

    monkeypatch.setattr(sdk_probe.anyio, "run", fail)
    monkeypatch.setattr(sys, "argv", ["check_mcp_session.py", "https://data.openecon.ai/mcp"])
    assert sdk_probe.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": False, "diagnostic": {"code": "verification_timeout", "stage": "session"}}


@pytest.mark.parametrize("failed_check", ["websites", "backends", "website_timeout"])
def test_fresh_non_mcp_failures_retain_their_real_failure_names(tmp_path, failed_check):
    report = complete_saved_report()
    name = "websites" if failed_check == "website_timeout" else failed_check
    report.update(status="FAILED", failed_checks=[name])
    if failed_check == "websites":
        report["checks"][name]["ok"] = False
        report["checks"][name]["results"][0]["http_status"] = 500
    elif failed_check == "backends":
        report["checks"][name]["ok"] = False
        report["checks"][name]["checks"][1]["ok"] = False
        report["checks"][name]["checks"][1]["error_type"] = "SECRET"
    else:
        report["checks"][name] = {"ok": False, "complete": False, "reason": "website_probe_timeout"}
    target = tmp_path / "latest.json"
    target.write_text(json.dumps(report))
    result = monitor.saved_status(target)
    assert result["reason"] == "checks_failed"
    assert result["failed_checks"] == [name]
    assert name in result["failure_details"]
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("contradiction", ["timeout", "error"])
def test_mcp_success_flags_cannot_override_timeout_or_error_diagnostic(monkeypatch, contradiction):
    body = {"ok": True, "head_status": 405, "initialized": True, "tools_listed": True,
            "query_data_available": True, "session_closed": True}
    if contradiction == "error":
        body["diagnostic"] = {"code": "capacity_rejected", "http_status": 503}
    monkeypatch.setattr(monitor, "run_probe", lambda *args: {
        "exit_code": 0, "output": json.dumps(body), "timed_out": contradiction == "timeout",
    })
    assert monitor.check_mcp()["ok"] is False


def test_unmodified_complete_mcp_success_contract_still_passes(monkeypatch):
    body = {"ok": True, "head_status": 405, "initialized": True, "tools_listed": True,
            "query_data_available": True, "session_closed": True}
    monkeypatch.setattr(monitor, "run_probe", lambda *args: {
        "exit_code": 0, "output": json.dumps(body), "timed_out": False,
    })
    assert monitor.check_mcp() == {"ok": True, "reason": "verified", "timed_out": False}
