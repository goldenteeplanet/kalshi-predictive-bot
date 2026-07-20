from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if current and "=" in line:
            key, value = line.split("=", 1)
            sections[current].setdefault(key, []).append(value)
    return sections


def certify_preview(service_path: Path, timer_path: Path) -> dict[str, Any]:
    service = parse_unit(service_path)
    timer = parse_unit(timer_path)
    service_values = service.get("Service", {})
    timer_values = timer.get("Timer", {})
    environment = service_values.get("Environment", [])
    exec_start = " ".join(service_values.get("ExecStart", []))
    gates = {
        "preview_suffixes": service_path.suffix == ".preview" and timer_path.suffix == ".preview",
        "not_installable": "Install" not in service and "Install" not in timer,
        "single_cycle": "--cycles 1" in exec_start and "--interval-minutes 0" in exec_start,
        "execution_disabled": "EXECUTION_ENABLED=false" in environment,
        "dry_run_and_kill_switch": all(
            value in environment
            for value in ("EXECUTION_DRY_RUN=true", "EXECUTION_KILL_SWITCH=true")
        ),
        "start_guard_present": bool(service_values.get("ExecStartPre")),
        "restart_disabled": service_values.get("Restart") == ["no"],
        "memory_bounded": service_values.get("MemoryHigh") == ["1800M"]
        and service_values.get("MemoryMax") == ["2200M"],
        "runtime_bounded": service_values.get("TimeoutStartSec") == ["45min"],
        "completion_relative_schedule": timer_values.get("OnUnitInactiveSec") == ["15min"]
        and "OnCalendar" not in timer_values,
        "activation_delay_safe": timer_values.get("OnActiveSec") == ["15min"]
        and "OnBootSec" not in timer_values,
        "missed_runs_not_replayed": timer_values.get("Persistent") == ["false"],
        "timer_target_exact": timer_values.get("Unit") == ["kalshi-r5-bounded.service"],
        "bounded_jitter": timer_values.get("RandomizedDelaySec") == ["30s"],
    }
    report: dict[str, Any] = {
        "phase": "R5-RECOVERY-9",
        "status": "PASSED_LOCAL_PREVIEW" if all(gates.values()) else "FAILED",
        "mode": "LOCAL_NO_START_PREVIEW",
        "cloud_access": False,
        "database_writes": 0,
        "service_changes": 0,
        "execution_enabled": False,
        "gates": gates,
        "service_path": str(service_path),
        "timer_path": str(timer_path),
        "rollback_plan": {
            "mode": "INERT_PREVIEW",
            "commands_executable": False,
            "required_evidence": [
                "verified code/config rollback bundle",
                "verified database backup",
                "three distinct passing R5-RECOVERY-6 cycles",
                "R5-RECOVERY-8 census passed",
                "fresh writer and lock clearance",
                "EXECUTION_ENABLED=false",
            ],
            "sequence": [
                "Stop only the bounded timer and wait for the bounded service to become inactive.",
                "Preserve failed-cycle and scheduler evidence.",
                "Restore the prior service and timer files from the verified rollback bundle.",
                "Run systemd verification and smoke checks without starting the legacy "
                "32-cycle service.",
                "Remain stopped pending explicit recovery approval.",
            ],
            "legacy_32_cycle_restart_allowed": False,
        },
        "deployment_requires_new_approval": True,
        "next_phase": "R5-RECOVERY-9 Deployment — Permanent Bounded Scheduler Cutover",
    }
    report["report_sha256"] = hashlib.sha256(
        (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    return report


def write_report(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
