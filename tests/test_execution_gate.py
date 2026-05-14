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
        active_plan_path=".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
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
        objective_status_path=".omx/reports/plan-objective-status.json",
        objective_status_exists=False,
        objective_status_plan_path=None,
        objective_status_all_complete=None,
        objective_status_completed_objectives=None,
        objective_status_total_objectives=None,
        objective_status_open_objectives=[],
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
    monkeypatch.setattr(module, "GLOBAL_RALPH_STATE_PATH", tmp_path / "missing-global-ralph-state.json")

    assert module._find_active_ralph_state() is None  # pylint: disable=protected-access


def test_find_active_ralph_state_reads_global_ralph_state(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    global_state = tmp_path / "state" / "ralph-state.json"
    global_state.parent.mkdir()
    global_state.write_text(
        json.dumps(
            {
                "active": True,
                "current_phase": "executing_goal_until_complete",
                "driving_plan": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
            }
        )
    )

    monkeypatch.setattr(module, "STATE_DIR", sessions_dir)
    monkeypatch.setattr(module, "GLOBAL_RALPH_STATE_PATH", global_state)

    payload = module._find_active_ralph_state()  # pylint: disable=protected-access

    assert payload is not None
    assert payload.get("driving_plan") == ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md"


def test_find_active_ralph_state_prefers_plan_bearing_state_over_newer_planless_state(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    sessions_dir = tmp_path / "sessions"
    plan_dir = sessions_dir / "with-plan"
    plan_dir.mkdir(parents=True)
    planless_dir = sessions_dir / "planless"
    planless_dir.mkdir(parents=True)

    (plan_dir / "ralph-state.json").write_text(
        json.dumps(
            {
                "active": True,
                "current_phase": "executing",
                "driving_plan": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
            }
        )
    )
    (planless_dir / "ralph-state.json").write_text(
        json.dumps(
            {
                "active": True,
                "current_phase": "executing",
            }
        )
    )
    older = 1000
    newer = 2000
    (plan_dir / "ralph-state.json").touch()
    (planless_dir / "ralph-state.json").touch()
    import os

    os.utime(plan_dir / "ralph-state.json", (older, older))
    os.utime(planless_dir / "ralph-state.json", (newer, newer))

    monkeypatch.setattr(module, "STATE_DIR", sessions_dir)

    payload = module._find_active_ralph_state()  # pylint: disable=protected-access

    assert payload is not None
    assert payload.get("driving_plan") == ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md"


def install_green_legacy_gate(monkeypatch, module, objective_status_path: Path) -> None:
    monkeypatch.setenv("EXECUTION_GATE_OBJECTIVE_STATUS_PATH", str(objective_status_path))
    monkeypatch.setattr(
        module,
        "_find_active_ralph_state",
        lambda: {
            "active": True,
            "current_phase": "executing",
            "driving_plan": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
        },
    )
    monkeypatch.setattr(
        module,
        "_read_manual_report",
        lambda: (
            module.ROOT / ".omx/reports/phase1-manual-10-chain-inprocess-real-keys.json",
            {"mode": "in_process_manual_10_chain_real_keys"},
            10,
            10,
            1.0,
            [],
        ),
    )
    monkeypatch.setattr(module, "_read_oracle_rates", lambda: {"baseline": 1.0, "alternative": 1.0})
    monkeypatch.setattr(module, "_tracked_worktree_dirty", lambda: False)


def test_active_plan_blocks_stop_when_objective_status_is_missing(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "missing-objectives.json"
    install_green_legacy_gate(monkeypatch, module, status_path)

    status = module.build_gate_status()

    assert status.can_stop is False
    assert status.objective_status_exists is False
    assert any("plan objective status is missing" in blocker for blocker in status.blockers)


def test_active_plan_blocks_stop_when_objectives_are_incomplete(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "objectives.json"
    status_path.write_text(
        json.dumps(
            {
                "plan_path": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
                "all_objectives_complete": False,
                "objectives": [
                    {"id": "score-balanced-smoke", "status": "complete"},
                    {"id": "run-full-30k", "status": "pending"},
                ],
            }
        )
    )
    install_green_legacy_gate(monkeypatch, module, status_path)

    status = module.build_gate_status()

    assert status.can_stop is False
    assert status.objective_status_completed_objectives == 1
    assert status.objective_status_total_objectives == 2
    assert status.objective_status_open_objectives == ["run-full-30k"]
    assert any("plan objectives remain incomplete (1/2 complete): run-full-30k" in blocker for blocker in status.blockers)


def test_active_plan_allows_stop_when_objectives_and_legacy_gates_are_green(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "objectives.json"
    status_path.write_text(
        json.dumps(
            {
                "plan_path": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
                "all_objectives_complete": True,
                "objectives": [
                    {"id": "score-balanced-smoke", "status": "complete"},
                    {"id": "run-full-30k", "status": "complete"},
                ],
                "goal_gate": {
                    "status": "claim_passed",
                    "claim_allowed": True,
                    "observed_success": 0.995,
                    "lower95": 0.991,
                    "provider_family_floors_green": True,
                    "adjudication_complete": True,
                    "production_replay_green": True,
                    "production_parity_green": True,
                    "blockers": [],
                },
            }
        )
    )
    install_green_legacy_gate(monkeypatch, module, status_path)

    status = module.build_gate_status()

    assert status.can_stop is True
    assert status.blockers == []
    assert status.objective_status_all_complete is True
    assert status.goal_gate_required is True
    assert status.goal_gate_claim_allowed is True


def test_reach99_plan_blocks_stop_when_claim_goal_gate_is_missing(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "objectives.json"
    status_path.write_text(
        json.dumps(
            {
                "plan_path": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
                "all_objectives_complete": True,
                "objectives": [
                    {"id": "score-balanced-smoke", "status": "complete"},
                    {"id": "run-full-30k", "status": "complete"},
                ],
            }
        )
    )
    install_green_legacy_gate(monkeypatch, module, status_path)

    status = module.build_gate_status()

    assert status.can_stop is False
    assert status.goal_gate_required is True
    assert status.goal_gate_exists is False
    assert any("claim-pass goal gate is missing" in blocker for blocker in status.blockers)


def test_reach99_plan_blocks_stop_on_denied_claim_goal_gate(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "objectives.json"
    status_path.write_text(
        json.dumps(
            {
                "plan_path": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
                "all_objectives_complete": True,
                "objectives": [
                    {"id": "score-balanced-smoke", "status": "complete"},
                    {"id": "run-full-30k", "status": "complete"},
                ],
                "goal_gate": {
                    "status": "denied",
                    "claim_allowed": False,
                    "observed_success": 0.923,
                    "lower95": 0.493,
                    "provider_family_floors_green": False,
                    "adjudication_complete": True,
                    "production_replay_green": False,
                    "production_parity_green": False,
                    "blockers": ["production parity has material drift"],
                },
            }
        )
    )
    install_green_legacy_gate(monkeypatch, module, status_path)

    status = module.build_gate_status()

    assert status.can_stop is False
    assert status.goal_gate_exists is True
    assert status.goal_gate_claim_allowed is False
    assert "claim_allowed is not true" in status.blockers
    assert any("production parity" in blocker for blocker in status.blockers)


def test_active_plan_complete_objectives_still_require_legacy_gates_green(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "objectives.json"
    status_path.write_text(
        json.dumps(
            {
                "plan_path": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
                "all_objectives_complete": True,
                "objectives": [
                    {"id": "score-balanced-smoke", "status": "verified_complete"},
                    {"id": "run-full-30k", "status": "verified_complete"},
                ],
            }
        )
    )
    install_green_legacy_gate(monkeypatch, module, status_path)
    monkeypatch.setattr(module, "_read_oracle_rates", lambda: {"baseline": 0.98, "alternative": 1.0})

    status = module.build_gate_status()

    assert status.can_stop is False
    assert "baseline strict oracle pass rate 0.980 is below required 0.990" in status.blockers


def test_active_plan_does_not_treat_legacy_green_status_as_objective_complete(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "objectives.json"
    status_path.write_text(
        json.dumps(
            {
                "plan_path": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
                "all_objectives_complete": True,
                "objectives": [{"id": "claim-grade-adjudication", "status": "green"}],
            }
        )
    )
    install_green_legacy_gate(monkeypatch, module, status_path)

    status = module.build_gate_status()

    assert status.can_stop is False
    assert status.objective_status_open_objectives == ["claim-grade-adjudication"]


def test_stop_hook_reports_incomplete_objectives(tmp_path, monkeypatch):
    module = load_execution_gate_module()
    status_path = tmp_path / "objectives.json"
    status_path.write_text(
        json.dumps(
            {
                "plan_path": ".omx/plans/plan-reach-99-all-330k-indicators-consensus.md",
                "all_objectives_complete": False,
                "objectives": [{"id": "production-parity", "status": "pending"}],
            }
        )
    )
    install_green_legacy_gate(monkeypatch, module, status_path)

    output = module.build_stop_hook_output(module.build_gate_status())

    assert output["decision"] == "block"
    assert "execution-gate: stop denied" in output["reason"]
    assert "production-parity" in output["reason"]
