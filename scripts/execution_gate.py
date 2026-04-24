#!/usr/bin/env python3
"""OMX execution gate for long-running Ralph sessions.

This script maintains machine-readable stop criteria so execution does not
silently stop while broader verification is still red.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / ".omx" / "reports"
STATE_DIR = ROOT / ".omx" / "state" / "sessions"
GATE_PATH = REPORTS_DIR / "execution-gate.json"
RED_FAMILIES_PATH = REPORTS_DIR / "red-families.json"
PLAN_OBJECTIVE_STATUS_PATH = REPORTS_DIR / "plan-objective-status.json"

MANUAL_REPORT_CANDIDATES = [
    REPORTS_DIR / "phase1-manual-10-chain-inprocess-real-keys.json",
    REPORTS_DIR / "phase1-manual-10-chain-inprocess.json",
]

ORACLE_REPORTS = {
    "baseline": REPORTS_DIR / "phase1-multiround-oracle-baseline-v3.json",
    "alternative": REPORTS_DIR / "phase1-multiround-oracle-alternative-v3.json",
}


@dataclass
class GateStatus:
    generated_at: str
    active_ralph: bool
    active_plan_path: str | None
    manual_report_path: str | None
    manual_report_mode: str | None
    manual_report_exists: bool
    manual_report_is_real_keys: bool
    manual_passed_chains: int | None
    manual_total_chains: int | None
    manual_pass_rate: float | None
    manual_required_pass_rate: float
    manual_required_total_chains: int
    oracle_report_paths: dict[str, str]
    oracle_pass_rates: dict[str, float | None]
    oracle_required_pass_rate: float
    objective_status_path: str
    objective_status_exists: bool
    objective_status_plan_path: str | None
    objective_status_all_complete: bool | None
    objective_status_completed_objectives: int | None
    objective_status_total_objectives: int | None
    objective_status_open_objectives: list[str]
    red_families: list[str]
    tracked_worktree_dirty: bool
    can_stop: bool
    blockers: list[str]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _find_active_ralph_state() -> dict[str, Any] | None:
    if not STATE_DIR.exists():
        return None

    latest: tuple[float, dict[str, Any]] | None = None
    for state_file in STATE_DIR.glob("*/ralph-state.json"):
        try:
            payload = json.loads(state_file.read_text())
        except Exception:
            continue
        if not _is_live_ralph_state(payload):
            continue
        mtime = state_file.stat().st_mtime
        if latest is None or mtime > latest[0]:
            latest = (mtime, payload)
    return latest[1] if latest else None


def _active_plan_path(active_state: dict[str, Any] | None) -> str | None:
    if not active_state:
        return None
    for key in ("driving_plan", "plan_path", "prd"):
        value = str(active_state.get(key) or "").strip()
        if value:
            return value
    return None


def _repo_relative_or_absolute(path_text: str | None) -> str | None:
    value = str(path_text or "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return str(path.resolve().relative_to(ROOT))
    except Exception:
        return str(path.resolve())


def _is_live_ralph_state(payload: dict[str, Any]) -> bool:
    """Return True when a Ralph state should still block stop.

    Old sessions can occasionally be left with ``active=true`` even after a
    completion/cancel handoff.  Treat those as inactive when they already have
    a terminal phase or a ``completed_at`` timestamp.
    """
    if not payload.get("active"):
        return False

    if payload.get("completed_at"):
        return False

    phase = str(payload.get("current_phase") or "").strip().lower()
    if phase in {"complete", "completed", "cancelled", "failed"}:
        return False

    return True


def _tracked_worktree_dirty() -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return True

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        if status in {"??", "!!"}:
            continue
        path = line[3:]
        if path.startswith(".codex/") or path.startswith("docs/testing/reports/"):
            continue
        return True
    return False


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _objective_text(objective: Any, index: int) -> str:
    if isinstance(objective, dict):
        return str(
            objective.get("id")
            or objective.get("name")
            or objective.get("title")
            or objective.get("description")
            or f"objective_{index}"
        )
    return str(objective or f"objective_{index}")


def _objective_is_complete(objective: Any) -> bool:
    if isinstance(objective, dict):
        if "complete" in objective:
            return bool(objective.get("complete"))
        status = str(objective.get("status") or "").strip().lower()
        return status in {"complete", "completed", "verified_complete"}
    return False


def _read_objective_status(path: Path = PLAN_OBJECTIVE_STATUS_PATH) -> dict[str, Any]:
    payload = _load_json(path)
    if payload is None:
        return {
            "exists": False,
            "plan_path": None,
            "all_complete": None,
            "completed": None,
            "total": None,
            "open": [],
        }

    objectives = payload.get("objectives") or []
    if not isinstance(objectives, list):
        objectives = []

    open_objectives = [
        _objective_text(objective, index)
        for index, objective in enumerate(objectives, start=1)
        if not _objective_is_complete(objective)
    ]
    completed = len(objectives) - len(open_objectives)
    explicit_all_complete = payload.get("all_objectives_complete")
    if explicit_all_complete is None:
        explicit_all_complete = payload.get("all_complete")
    all_complete = bool(explicit_all_complete) and bool(objectives) and not open_objectives

    return {
        "exists": True,
        "plan_path": str(payload.get("plan_path") or payload.get("driving_plan") or "").strip() or None,
        "all_complete": all_complete,
        "completed": completed,
        "total": len(objectives),
        "open": open_objectives,
    }


def _choose_manual_report() -> tuple[Path | None, dict[str, Any] | None]:
    ranked: list[tuple[int, float, Path, dict[str, Any]]] = []
    for priority, candidate in enumerate(MANUAL_REPORT_CANDIDATES):
        payload = _load_json(candidate)
        if payload is None:
            continue
        try:
            mtime = candidate.stat().st_mtime
        except Exception:
            mtime = 0.0
        ranked.append((priority, mtime, candidate, payload))

    if not ranked:
        return None, None

    ranked.sort(key=lambda item: (item[0], -item[1]))
    _, _, path, payload = ranked[0]
    return path, payload


def _read_manual_report() -> tuple[Path | None, dict[str, Any] | None, int | None, int | None, float | None, list[str]]:
    path, payload = _choose_manual_report()
    if path is None or payload is None:
        return None, None, None, None, None, []

    passed = payload.get("passed_chains")
    total = payload.get("total_chains")
    pass_rate = payload.get("pass_rate")
    red = [
        str(result.get("name"))
        for result in payload.get("results", [])
        if not result.get("passed")
    ]
    return path, payload, passed, total, pass_rate, red


def _read_oracle_rates() -> dict[str, float | None]:
    rates: dict[str, float | None] = {}
    for label, path in ORACLE_REPORTS.items():
        payload = _load_json(path)
        if payload is None:
            rates[label] = None
            continue
        raw_rate = payload.get("strict_pass_rate_ratio")
        try:
            rates[label] = float(raw_rate) if raw_rate is not None else None
        except (TypeError, ValueError):
            rates[label] = None
    return rates


def build_gate_status() -> GateStatus:
    active_state = _find_active_ralph_state()
    active_ralph = active_state is not None
    active_plan_path = _active_plan_path(active_state)
    manual_path, manual_payload, manual_passed, manual_total, manual_pass_rate, red_families = (
        _read_manual_report()
    )
    manual_required_rate = float(os.getenv("EXECUTION_GATE_MIN_MANUAL_PASS_RATE", "1.0"))
    manual_required_total = int(os.getenv("EXECUTION_GATE_REQUIRED_MANUAL_TOTAL_CHAINS", "10"))
    oracle_required_rate = float(os.getenv("EXECUTION_GATE_MIN_ORACLE_PASS_RATE", "0.99"))
    oracle_rates = _read_oracle_rates()
    objective_status_path = Path(
        os.getenv("EXECUTION_GATE_OBJECTIVE_STATUS_PATH", str(PLAN_OBJECTIVE_STATUS_PATH))
    )
    if not objective_status_path.is_absolute():
        objective_status_path = ROOT / objective_status_path
    objective_status = _read_objective_status(objective_status_path)
    dirty = _tracked_worktree_dirty()

    manual_mode = str(manual_payload.get("mode")) if manual_payload else None
    manual_is_real_keys = bool(
        manual_path
        and (
            "real-keys" in manual_path.name
            or "real_keys" in (manual_mode or "")
        )
    )

    blockers: list[str] = []
    if active_ralph and manual_path is None:
        blockers.append("authoritative manual verification report is missing")
    if active_ralph and manual_path is not None and not manual_is_real_keys:
        blockers.append("authoritative real-key manual verification report is missing")
    if active_ralph and manual_total is not None and manual_total < manual_required_total:
        blockers.append(
            f"authoritative real-key manual family pack is incomplete ({manual_total}/{manual_required_total} chains)"
        )
    if active_ralph and manual_pass_rate is not None and manual_pass_rate < manual_required_rate:
        blockers.append(
            f"manual verification pass rate {manual_pass_rate:.3f} is below required {manual_required_rate:.3f}"
        )
    if active_ralph and red_families:
        blockers.append(f"red families remain: {', '.join(red_families)}")
    if active_ralph and active_plan_path:
        active_plan_normalized = _repo_relative_or_absolute(active_plan_path)
        objective_plan_normalized = _repo_relative_or_absolute(objective_status.get("plan_path"))
        if not objective_status["exists"]:
            blockers.append(
                f"plan objective status is missing for {active_plan_normalized}; "
                f"write {objective_status_path} only after all plan objectives are verified complete"
            )
        elif objective_plan_normalized != active_plan_normalized:
            blockers.append(
                "plan objective status does not match active Ralph plan "
                f"({objective_plan_normalized or '<missing>'} != {active_plan_normalized})"
            )
        elif not objective_status["all_complete"]:
            open_objectives = objective_status["open"]
            completed = objective_status["completed"]
            total = objective_status["total"]
            if open_objectives:
                blockers.append(
                    "plan objectives remain incomplete "
                    f"({completed}/{total} complete): {', '.join(open_objectives[:8])}"
                )
            else:
                blockers.append(
                    "plan objective status is not explicitly all_objectives_complete=true "
                    f"({completed}/{total} complete)"
                )
    if active_ralph:
        for label, rate in oracle_rates.items():
            if rate is None:
                blockers.append(f"{label} strict oracle report is missing or unreadable")
            elif rate < oracle_required_rate:
                blockers.append(
                    f"{label} strict oracle pass rate {rate:.3f} is below required {oracle_required_rate:.3f}"
                )
    if active_ralph and dirty:
        blockers.append("tracked worktree still contains uncommitted changes")

    can_stop = not blockers
    return GateStatus(
        generated_at=_utc_now(),
        active_ralph=active_ralph,
        active_plan_path=active_plan_path,
        manual_report_path=str(manual_path) if manual_path else None,
        manual_report_mode=manual_mode,
        manual_report_exists=manual_path is not None,
        manual_report_is_real_keys=manual_is_real_keys,
        manual_passed_chains=manual_passed,
        manual_total_chains=manual_total,
        manual_pass_rate=manual_pass_rate,
        manual_required_pass_rate=manual_required_rate,
        manual_required_total_chains=manual_required_total,
        oracle_report_paths={label: str(path) for label, path in ORACLE_REPORTS.items()},
        oracle_pass_rates=oracle_rates,
        oracle_required_pass_rate=oracle_required_rate,
        objective_status_path=str(objective_status_path),
        objective_status_exists=bool(objective_status["exists"]),
        objective_status_plan_path=objective_status["plan_path"],
        objective_status_all_complete=objective_status["all_complete"],
        objective_status_completed_objectives=objective_status["completed"],
        objective_status_total_objectives=objective_status["total"],
        objective_status_open_objectives=list(objective_status["open"]),
        red_families=red_families,
        tracked_worktree_dirty=dirty,
        can_stop=can_stop,
        blockers=blockers,
    )


def write_gate_files(status: GateStatus) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    GATE_PATH.write_text(json.dumps(asdict(status), indent=2) + "\n")
    RED_FAMILIES_PATH.write_text(
        json.dumps(
            {
                "generated_at": status.generated_at,
                "red_families": status.red_families,
                "manual_report_path": status.manual_report_path,
                "manual_report_mode": status.manual_report_mode,
            },
            indent=2,
        )
        + "\n"
    )


def build_stop_hook_output(status: GateStatus) -> dict[str, Any]:
    """Return a Codex Stop-hook-compatible JSON payload.

    Stop hooks are special: exit code 0 must emit JSON on stdout, not plain text.
    When the execution gate is green we simply allow the stop to proceed. When the
    gate is red we ask Codex to continue with a concise blocker summary.
    """
    if status.can_stop:
        return {"continue": True}

    blocker_lines = "\n".join(f"- {blocker}" for blocker in status.blockers)
    reason = "execution-gate: stop denied"
    if blocker_lines:
        reason = f"{reason}\n{blocker_lines}"
    return {"decision": "block", "reason": reason}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write gate artifacts and exit 0")
    parser.add_argument(
        "--check-stop",
        action="store_true",
        help="write gate artifacts and exit non-zero if stop is not allowed",
    )
    parser.add_argument(
        "--hook-stop-json",
        action="store_true",
        help="write gate artifacts and emit Stop-hook-compatible JSON on stdout",
    )
    args, _unknown_args = parser.parse_known_args()

    status = build_gate_status()
    write_gate_files(status)

    if args.hook_stop_json:
        print(json.dumps(build_stop_hook_output(status)))
        return 0

    if args.check_stop:
        if status.can_stop:
            print("execution-gate: stop allowed")
            return 0
        print("execution-gate: stop denied")
        for blocker in status.blockers:
            print(f"- {blocker}")
        return 2

    if args.write:
        print(GATE_PATH)
        return 0

    print(json.dumps(asdict(status), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
