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

OPENECON_HOSTS = tuple(sorted(host for host in websites.HOSTS if websites.site_group(host) == "openecon"))
# A probed non-OpenEcon vhost that is expected UP (not in KNOWN_DOWN), so its
# 503 is a real "other"-group failure rather than accepted known degradation.
OTHER_FAILING_HOST = "crypto.hansearch.com"


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
    # Known-down hosts are outside the OpenEcon group: the new failure stays
    # visible (not folded into known_degraded) but is non-fatal.
    assert summary["ok"] is True
    assert summary["failed_checks"] == []
    assert summary["other_failed_checks"] == [f"{row['host']}:{row['scheme']}:IPv{row['ip_family']}"]


@pytest.mark.parametrize("field,value", [
    ("tls_verified", False), ("canonical_target_verified", False),
    ("curl_exit_code", 28), ("http_status", 500), ("http_status", 503),
])
@pytest.mark.parametrize("host", OPENECON_HOSTS)
def test_any_openecon_group_row_failure_is_fatal(host, field, value):
    report = website_report(degraded=True)
    next(row for row in report["results"] if row["host"] == host)[field] = value
    summary = monitor.website_summary(report)
    assert summary["ok"] is False
    assert len(summary["failed_checks"]) == 1 and summary["failed_checks"][0].startswith(host + ":")
    assert summary["other_failed_checks"] == []


@pytest.mark.parametrize("host,fatal", [("data.openecon.ai", True), ("luhan.io", False), (OTHER_FAILING_HOST, False)])
def test_new_503_is_a_regression_fatal_only_in_openecon_group(host, fatal):
    report = website_report()
    next(row for row in report["results"] if row["host"] == host)["http_status"] = 503
    summary = monitor.website_summary(report)
    assert summary["ok"] is (not fatal)
    assert bool(summary["failed_checks"]) is fatal
    assert bool(summary["other_failed_checks"]) is (not fatal)


def test_site_groups_are_derived_from_canonical_targets():
    assert set(OPENECON_HOSTS) == {"data.openecon.ai", "openecon.ai", "www.openecon.ai", "deepecon.ai", "www.deepecon.ai"}
    assert websites.site_group("jp.hansearch.com") == "other"
    # jp is owner-retired (KNOWN_DOWN since 2026-11-07); the failing-"other"
    # fixture host below must be one whose 503 is NOT already accepted.
    assert "jp.hansearch.com" in websites.KNOWN_DOWN
    assert OTHER_FAILING_HOST not in websites.KNOWN_DOWN
    assert websites.site_group(OTHER_FAILING_HOST) == "other"
    assert all(websites.site_group(host) in ("openecon", "other") for host in websites.HOSTS)


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


def run_main_with_website_rows(tmp_path, monkeypatch, failing_host, child_exit=None):
    """Drive main() through the real website summary with failing host(s)."""
    report = website_report(degraded=True)
    failing = {failing_host} if isinstance(failing_host, str) else set(failing_host or ())
    for row in report["results"]:
        if row["host"] in failing:
            row["http_status"] = 503
    exit_code = child_exit if child_exit is not None else (1 if failing else 0)

    def fake_probe(arguments, timeout):
        assert arguments[1].endswith("check_websites.py")
        Path(arguments[3]).write_text(json.dumps(report))
        return {"exit_code": exit_code, "output": "", "timed_out": False}

    target = tmp_path / "latest.json"
    monkeypatch.setattr(monitor, "run_probe", fake_probe)
    monkeypatch.setattr(monitor, "check_mcp", lambda: {"ok": True, "reason": "verified", "timed_out": False})
    monkeypatch.setattr(monitor, "check_backends", lambda: {"ok": True, "checks": [{"port": p, "ok": True} for p in (3001, 3002)]})
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--output", str(target)])
    return monitor.main(), target


@pytest.mark.parametrize("host", [OTHER_FAILING_HOST, "luhan.io", "api.hansearch.com"])
def test_failing_non_openecon_host_exits_zero_as_degraded_other(tmp_path, monkeypatch, capsys, host):
    exit_code, target = run_main_with_website_rows(tmp_path, monkeypatch, host)
    assert exit_code == 0
    report = json.loads(target.read_text())
    assert report["status"] == "DEGRADED_OTHER"
    assert report["failed_checks"] == []
    assert sorted({label.split(":")[0] for label in report["other_failed_checks"]}) == [host]
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "DEGRADED_OTHER"
    assert "failure_details" not in printed
    # The non-fatal outage keeps its bounded probe evidence in the journal.
    evidence = printed["other_failure_details"]["websites"]
    assert evidence["failed_checks"] == report["other_failed_checks"]
    assert [probe["host"] for probe in evidence["failed_probe_details"]] == [host] * 4
    assert all(probe["http_status"] == 503 for probe in evidence["failed_probe_details"])
    saved = monitor.saved_status(target)
    assert saved["status"] == "DEGRADED_OTHER"
    assert len(saved["other_failed_checks"]) == 4
    assert saved["other_failure_details"] == printed["other_failure_details"]
    assert "failure_details" not in saved
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--status", "--output", str(target)])
    assert monitor.main() == 0


@pytest.mark.parametrize("host", OPENECON_HOSTS)
def test_failing_openecon_host_exits_one_even_with_other_failures(tmp_path, monkeypatch, host):
    exit_code, target = run_main_with_website_rows(tmp_path, monkeypatch, (host, OTHER_FAILING_HOST))
    assert exit_code == 1
    report = json.loads(target.read_text())
    assert report["status"] == "FAILED"
    assert report["failed_checks"] == ["websites"]
    assert {label.split(":")[0] for label in report["other_failed_checks"]} == {OTHER_FAILING_HOST}
    saved = monitor.saved_status(target)
    assert saved["status"] == "FAILED"
    fatal = saved["failure_details"]["websites"]
    assert len(fatal["failed_checks"]) == 4
    assert all(label.startswith(host + ":") for label in fatal["failed_checks"])
    assert {probe["host"] for probe in fatal["failed_probe_details"]} == {host}
    other = saved["other_failure_details"]["websites"]
    assert {probe["host"] for probe in other["failed_probe_details"]} == {OTHER_FAILING_HOST}


def test_child_exit_contradicting_rows_fails_closed(tmp_path, monkeypatch):
    exit_code, target = run_main_with_website_rows(tmp_path, monkeypatch, OTHER_FAILING_HOST, child_exit=0)
    assert exit_code == 1
    assert json.loads(target.read_text())["checks"]["websites"]["reason"] == "website_probe_failed"


def test_saved_degraded_other_with_contradictory_other_list_is_invalid(tmp_path, monkeypatch):
    _, target = run_main_with_website_rows(tmp_path, monkeypatch, OTHER_FAILING_HOST)
    report = json.loads(target.read_text())
    report["other_failed_checks"] = []
    target.write_text(json.dumps(report))
    assert monitor.saved_status(target) == {"status": "FAILED", "reason": "invalid_report"}


KNOWN_DOWN_HOST = "jp.hansearch.com"  # owner-retired, kept in KNOWN_DOWN


def run_main_with_known_down_row(tmp_path, monkeypatch, field, value):
    """main() over the real summary with ONE known-down row changed."""
    report = website_report(degraded=True)
    row = next(row for row in report["results"] if row["host"] == KNOWN_DOWN_HOST)
    row[field] = value
    child_failed = not (row["http_status"] == 503 and row["curl_exit_code"] == 0
                        and row["tls_verified"] and row["canonical_target_verified"])

    def fake_probe(arguments, timeout):
        Path(arguments[3]).write_text(json.dumps(report))
        return {"exit_code": 1 if child_failed else 0, "output": "", "timed_out": False}

    target = tmp_path / "latest.json"
    monkeypatch.setattr(monitor, "run_probe", fake_probe)
    monkeypatch.setattr(monitor, "check_mcp", lambda: {"ok": True, "reason": "verified", "timed_out": False})
    monkeypatch.setattr(monitor, "check_backends", lambda: {"ok": True, "checks": [{"port": p, "ok": True} for p in (3001, 3002)]})
    monkeypatch.setattr(sys, "argv", ["monitor.py", "--output", str(target)])
    return monitor.main(), json.loads(target.read_text())


def test_known_down_contract_503_is_accepted_degradation(tmp_path, monkeypatch):
    assert KNOWN_DOWN_HOST in websites.KNOWN_DOWN
    exit_code, report = run_main_with_known_down_row(tmp_path, monkeypatch, "http_status", 503)
    assert exit_code == 0
    assert report["status"] == "DEGRADED"
    assert KNOWN_DOWN_HOST in report["known_degraded"]
    assert report["other_failed_checks"] == [] and report["failed_checks"] == []


@pytest.mark.parametrize("field,value", [
    ("curl_exit_code", 28), ("curl_exit_code", 7), ("tls_verified", False),
    ("canonical_target_verified", False), ("http_status", 500), ("http_status", 404),
])
def test_known_down_contract_other_failure_is_degraded_other_and_non_fatal(tmp_path, monkeypatch, field, value):
    # KNOWN_DOWN accepts exactly "503 over a good transport"; any other
    # failure on that host stays visible (DEGRADED_OTHER) but never pages.
    exit_code, report = run_main_with_known_down_row(tmp_path, monkeypatch, field, value)
    assert exit_code == 0
    assert report["status"] == "DEGRADED_OTHER"
    assert report["failed_checks"] == []
    assert {label.split(":")[0] for label in report["other_failed_checks"]} == {KNOWN_DOWN_HOST}


def test_healthy_run_still_reports_known_degradation(tmp_path, monkeypatch):
    exit_code, target = run_main_with_website_rows(tmp_path, monkeypatch, None)
    assert exit_code == 0
    assert json.loads(target.read_text())["status"] == "DEGRADED"


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
        next(row for row in report["checks"][name]["results"] if row["host"] in OPENECON_HOSTS)["http_status"] = 500
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


PROBE_KEYS = (
    "host", "scheme", "ip_family", "curl_exit_code", "http_status",
    "tls_verified", "canonical_target_verified", "elapsed_seconds",
)


def poison_two_rows(check, host):
    rows = [row for row in check["results"] if row["host"] == host][:2]
    rows[0].update(curl_exit_code=28, http_status=0, tls_verified=False, elapsed_seconds=12.001)
    rows[1].update(http_status=500, elapsed_seconds=0.3)
    for row in rows:
        row.update(stderr="SECRET", url_effective="https://SECRET/", body="SECRET", diagnostic="SECRET")
    return rows


def test_saved_website_failure_preserves_only_failed_probe_diagnostics(tmp_path):
    report = complete_saved_report(degraded=True)
    report.update(status="FAILED", failed_checks=["websites"])
    check = report["checks"]["websites"]
    check["ok"] = False
    rows = poison_two_rows(check, "data.openecon.ai")
    target = tmp_path / "latest.json"
    target.write_text(json.dumps(report))

    result = monitor.saved_status(target)

    assert result["status"] == "FAILED"
    assert result["failed_checks"] == ["websites"]
    assert result["known_degraded"] == sorted(websites.KNOWN_DOWN)
    detail = result["failure_details"]["websites"]
    assert len(detail["failed_checks"]) == len(detail["failed_probe_details"]) == 2
    assert detail["failed_probe_details"] == [{key: row[key] for key in PROBE_KEYS} for row in rows]
    assert "other_failure_details" not in result
    assert "SECRET" not in json.dumps(result)


def test_saved_other_group_failure_preserves_probe_evidence_without_failing(tmp_path):
    report = complete_saved_report(degraded=True)
    check = report["checks"]["websites"]
    rows = poison_two_rows(check, "luhan.io")
    labels = [f"{row['host']}:{row['scheme']}:IPv{row['ip_family']}" for row in rows]
    report.update(status="DEGRADED_OTHER", other_failed_checks=labels)
    target = tmp_path / "latest.json"
    target.write_text(json.dumps(report))

    result = monitor.saved_status(target)

    assert result["status"] == "DEGRADED_OTHER"
    assert result["failed_checks"] == []
    assert "failure_details" not in result
    detail = result["other_failure_details"]["websites"]
    assert detail["failed_checks"] == labels
    assert detail["failed_probe_details"] == [{key: row[key] for key in PROBE_KEYS} for row in rows]
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [
    ("curl_exit_code", "SECRET"), ("curl_exit_code", True),
    ("curl_exit_code", -1), ("curl_exit_code", 256), ("curl_exit_code", 28.0),
    ("http_status", "SECRET"), ("http_status", True),
    ("http_status", -1), ("http_status", 99), ("http_status", 600),
    ("http_status", 500.0),
    ("tls_verified", "SECRET"), ("tls_verified", 1),
    ("canonical_target_verified", "SECRET"), ("canonical_target_verified", 0),
    ("elapsed_seconds", "SECRET"), ("elapsed_seconds", True),
    ("elapsed_seconds", -1), ("elapsed_seconds", 61),
    ("elapsed_seconds", float("nan")), ("elapsed_seconds", float("inf")),
    ("elapsed_seconds", -float("inf")), ("elapsed_seconds", 10 ** 1000),
])
@pytest.mark.parametrize("group", ["openecon", "other"])
def test_website_failure_diagnostics_drop_invalid_field_types_and_ranges(group, field, value):
    check = website_report()
    check["ok"] = False
    row = next(row for row in check["results"] if websites.site_group(row["host"]) == group)
    row.update(http_status=500, canonical_target_verified=False)
    row[field] = value

    if group == "openecon":
        details = monitor.failure_details({"websites": check})
    else:
        details = monitor.other_failure_details(check)

    probe = details["websites"]["failed_probe_details"][0]
    assert probe["host"] == row["host"]
    assert field not in probe
    assert "SECRET" not in json.dumps(details, allow_nan=False)


@pytest.mark.parametrize("field", ["host", "scheme", "ip_family"])
def test_unknown_website_jobs_cannot_be_reflected_in_failure_diagnostics(field):
    check = website_report()
    check["ok"] = False
    check["results"][0].update(http_status=500)
    check["results"][0][field] = "SECRET"

    details = monitor.failure_details({"websites": check})

    assert details == {"websites": {"reason": "probe_failed"}}
    assert monitor.other_failure_details(check) is None
    assert "SECRET" not in json.dumps(details)


def test_website_failure_diagnostics_remain_bounded_to_exact_inventory():
    check = website_report()
    check["ok"] = False
    for row in check["results"]:
        row.update(curl_exit_code=28, http_status=0)
    fatal = monitor.failure_details({"websites": check})["websites"]["failed_probe_details"]
    other = monitor.other_failure_details(check)["websites"]["failed_probe_details"]
    assert len(fatal) == 4 * len(OPENECON_HOSTS)
    assert {probe["host"] for probe in fatal} == set(OPENECON_HOSTS)
    assert len(fatal) + len(other) == len(websites.JOBS)
    assert not {probe["host"] for probe in other} & set(OPENECON_HOSTS)

    check["results"].append(copy.deepcopy(check["results"][0]))
    assert monitor.failure_details({"websites": check}) == {"websites": {"reason": "probe_failed"}}
    assert monitor.other_failure_details(check) is None


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
