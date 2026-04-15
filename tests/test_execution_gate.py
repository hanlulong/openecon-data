from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "execution_gate.py"


def load_execution_gate_module():
    spec = importlib.util.spec_from_file_location("execution_gate_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_status(module, *, can_stop: bool, blockers: list[str] | None = None):
    return module.GateStatus(
        generated_at="2026-04-14T00:00:00Z",
        active_ralph=True,
        manual_report_path=None,
        manual_report_mode=None,
        manual_report_exists=False,
        manual_report_is_real_keys=False,
        manual_passed_chains=None,
        manual_total_chains=None,
        manual_pass_rate=None,
        manual_required_pass_rate=1.0,
        manual_required_total_chains=10,
        oracle_report_paths={"baseline": "baseline.json", "alternative": "alternative.json"},
        oracle_pass_rates={"baseline": None, "alternative": None},
        oracle_required_pass_rate=0.99,
        red_families=[],
        tracked_worktree_dirty=False,
        can_stop=can_stop,
        blockers=blockers or [],
    )


def test_build_stop_hook_output_allows_stop_when_gate_is_green():
    module = load_execution_gate_module()

    output = module.build_stop_hook_output(make_status(module, can_stop=True))

    assert output == {"continue": True}


def test_build_stop_hook_output_blocks_with_blocker_summary():
    module = load_execution_gate_module()

    output = module.build_stop_hook_output(
        make_status(
            module,
            can_stop=False,
            blockers=[
                "baseline strict oracle report is missing or unreadable",
                "tracked worktree still contains uncommitted changes",
            ],
        )
    )

    assert output["decision"] == "block"
    assert "execution-gate: stop denied" in output["reason"]
    assert "- baseline strict oracle report is missing or unreadable" in output["reason"]
    assert "- tracked worktree still contains uncommitted changes" in output["reason"]


def test_hook_stop_json_mode_prints_valid_json(monkeypatch, capsys):
    module = load_execution_gate_module()
    status = make_status(module, can_stop=False, blockers=["manual verification report is missing"])

    monkeypatch.setattr(module, "build_gate_status", lambda: status)
    monkeypatch.setattr(module, "write_gate_files", lambda _: None)
    monkeypatch.setattr(sys, "argv", ["execution_gate.py", "--hook-stop-json"])

    exit_code = module.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload == {
        "decision": "block",
        "reason": "execution-gate: stop denied\n- manual verification report is missing",
    }


def test_hook_stop_json_mode_ignores_unknown_args(monkeypatch, capsys):
    module = load_execution_gate_module()
    status = make_status(module, can_stop=False, blockers=["tracked worktree still contains uncommitted changes"])

    monkeypatch.setattr(module, "build_gate_status", lambda: status)
    monkeypatch.setattr(module, "write_gate_files", lambda _: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["execution_gate.py", "--hook-stop-json", "--unexpected-arg", "payload.json"],
    )

    exit_code = module.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload == {
        "decision": "block",
        "reason": "execution-gate: stop denied\n- tracked worktree still contains uncommitted changes",
    }
