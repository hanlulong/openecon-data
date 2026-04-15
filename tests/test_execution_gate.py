from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


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


def test_tracked_worktree_dirty_ignores_untracked_files(monkeypatch):
    module = load_execution_gate_module()

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="?? .codex/agents/\n?? docs/design/SESSION_RESTART_2026-04-15_REBOOT.md\n",
        ),
    )

    assert module._tracked_worktree_dirty() is False  # pylint: disable=protected-access


def test_tracked_worktree_dirty_detects_real_tracked_modification(monkeypatch):
    module = load_execution_gate_module()

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=" M backend/services/query.py\n?? .codex/agents/\n",
        ),
    )

    assert module._tracked_worktree_dirty() is True  # pylint: disable=protected-access


def test_find_active_ralph_state_ignores_stale_terminal_active_files(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    sessions_dir = tmp_path / "sessions"
    stale_dir = sessions_dir / "old"
    stale_dir.mkdir(parents=True)
    fresh_dir = sessions_dir / "fresh"
    fresh_dir.mkdir(parents=True)

    (stale_dir / "ralph-state.json").write_text(
        json.dumps(
            {
                "active": True,
                "current_phase": "executing",
                "completed_at": "2026-04-15T20:49:30Z",
            }
        )
    )
    (fresh_dir / "ralph-state.json").write_text(
        json.dumps(
            {
                "active": True,
                "current_phase": "executing",
            }
        )
    )

    monkeypatch.setattr(module, "STATE_DIR", sessions_dir)

    payload = module._find_active_ralph_state()  # pylint: disable=protected-access

    assert payload is not None
    assert payload.get("completed_at") is None


def test_find_active_ralph_state_returns_none_when_only_terminal_active_files_exist(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    sessions_dir = tmp_path / "sessions"
    stale_dir = sessions_dir / "stale"
    stale_dir.mkdir(parents=True)
    (stale_dir / "ralph-state.json").write_text(
        json.dumps(
            {
                "active": True,
                "current_phase": "complete",
                "completed_at": "2026-04-15T20:49:30Z",
            }
        )
    )

    monkeypatch.setattr(module, "STATE_DIR", sessions_dir)

    assert module._find_active_ralph_state() is None  # pylint: disable=protected-access
