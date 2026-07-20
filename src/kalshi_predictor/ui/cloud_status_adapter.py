from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from kalshi_predictor.ui.progress import VALID_STATES


EXPECTED_DATABASE_PATH = "/var/lib/kalshi-bot/kalshi_phase1.db"
REQUIRED_WORKSTREAMS = ("PMB evaluation", "PROV attribution", "NYC weather", "GH liquidity", "Paper readiness")


def _utc(value: Any, label: str, diagnostics: list[str]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        diagnostics.append(f"TIMESTAMP_INVALID:{label}")
        return None
    if parsed.tzinfo is None:
        diagnostics.append(f"TIMESTAMP_NAIVE:{label}")
        return None
    return parsed.astimezone(UTC)


def _state(value: Any, label: str, diagnostics: list[str]) -> str:
    state = str(value or "BLOCKED").upper()
    if state not in VALID_STATES:
        diagnostics.append(f"STATE_INVALID:{label}:{state}")
        return "BLOCKED"
    return state


def adapt_cloud_status_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics: list[str] = []
    collected_at = _utc(bundle.get("collected_at"), "collected_at", diagnostics)
    sources = bundle.get("sources") or {}
    writer = sources.get("db_writer_monitor") or {}
    locks = sources.get("db_locks") or {}
    backup = sources.get("backup_report") or {}
    scheduler = sources.get("scheduler") or {}
    execution = sources.get("execution") or {}
    process = sources.get("process") or {}

    if writer.get("database_path") != EXPECTED_DATABASE_PATH:
        diagnostics.append("DATABASE_PATH_MISMATCH")
    writer_active = writer.get("current_writer_pid") not in (None, "")
    lock_writer_active = bool(locks.get("writer_active"))
    if writer_active != lock_writer_active:
        diagnostics.append("WRITER_LOCK_CONTRADICTION")
    if bool(writer.get("safe_to_start_write")) and writer_active:
        diagnostics.append("WRITER_SAFETY_CONTRADICTION")
    if process.get("pid") and writer_active and process.get("pid") != writer.get("current_writer_pid"):
        diagnostics.append("PROCESS_WRITER_PID_MISMATCH")
    if execution.get("execution_enabled") is not False:
        diagnostics.append("EXECUTION_NOT_EXPLICITLY_DISABLED")

    backup_complete = (
        backup.get("integrity_check") == "ok"
        and isinstance(backup.get("sha256"), str) and len(backup["sha256"]) == 64
        and backup.get("backup_path")
    )
    if backup and not backup_complete:
        diagnostics.append("BACKUP_EVIDENCE_INCOMPLETE")

    source_times = bundle.get("source_timestamps") or {}
    if collected_at:
        for name in ("db_writer_monitor", "db_locks", "scheduler", "execution"):
            source_time = _utc(source_times.get(name), f"source:{name}", diagnostics)
            if source_time and (collected_at - source_time).total_seconds() > 120:
                diagnostics.append(f"SOURCE_STALE:{name}")
            if source_time and source_time > collected_at:
                diagnostics.append(f"SOURCE_FROM_FUTURE:{name}")

    reports = []
    for report in bundle.get("reports") or []:
        state = _state(report.get("state"), f"report:{report.get('phase')}", diagnostics)
        normalized = {
            "phase": report.get("phase") or "UNKNOWN",
            "state": state,
            "path": report.get("path") or "missing",
            "generated_at": report.get("generated_at") or "unknown",
        }
        if state == "PASSED" and not report.get("verified"):
            normalized["state"] = "BLOCKED"
            diagnostics.append(f"REPORT_PASS_UNVERIFIED:{normalized['phase']}")
        reports.append(normalized)

    process_state = _state(process.get("state") or ("RUNNING" if writer_active else "WAITING"), "process", diagnostics)
    completion_evidence = process.get("completion_evidence")
    if process_state == "PASSED" and (
        not completion_evidence or completion_evidence not in {item["path"] for item in reports if item["state"] == "PASSED"}
    ):
        process_state = "BLOCKED"
        diagnostics.append("PROCESS_COMPLETION_EVIDENCE_INVALID")

    phase_rows = bundle.get("workstreams") or []
    workstreams = []
    names = set()
    for item in phase_rows:
        name = item.get("name") or "Unknown"
        names.add(name)
        workstreams.append({
            "name": name,
            "state": _state(item.get("state"), f"workstream:{name}", diagnostics),
            "current_phase": item.get("current_phase") or "Unspecified",
            "completed": list(item.get("completed") or []),
            "blocked": list(item.get("blocked") or []),
            "next_safe_phase": item.get("next_safe_phase") or "No certified next phase",
        })
    diagnostics.extend(f"WORKSTREAM_MISSING:{name}" for name in REQUIRED_WORKSTREAMS if name not in names)

    total_cycles = scheduler.get("total_cycles")
    current_cycle = scheduler.get("current_cycle")
    cycle_label = f"{current_cycle} / {total_cycles}" if current_cycle and total_cycles else "unknown"
    snapshot = {
        "generated_at": collected_at.isoformat().replace("+00:00", "Z") if collected_at else None,
        "execution_enabled": execution.get("execution_enabled") is True,
        "paper_enabled": execution.get("paper_enabled") is True,
        "active_process": {
            "name": process.get("name") or writer.get("current_writer_command") or "No certified active process",
            "pid": process.get("pid") or writer.get("current_writer_pid"),
            "runtime": process.get("runtime") or writer.get("current_writer_elapsed") or "n/a",
            "stage": process.get("stage") or writer.get("long_job_stage") or "unknown",
            "state": process_state,
            "progress_percent": process.get("progress_percent"),
            "estimated_remaining": process.get("estimated_remaining"),
            "completion_evidence": completion_evidence,
            "started_at": process.get("started_at"),
            "updated_at": process.get("updated_at") or process.get("observed_at"),
            "completed_units": process.get("completed_units"),
            "total_units": process.get("total_units"),
        },
        "writer": {
            "state": "RUNNING" if writer_active else "WAITING",
            "pid": writer.get("current_writer_pid"),
            "safe_to_start_write": bool(writer.get("safe_to_start_write")) and not diagnostics,
            "lock_status": locks.get("status") or writer.get("diagnostics_status") or "UNKNOWN",
        },
        "backup": {
            "state": "PASSED" if backup_complete else "WAITING",
            "integrity": backup.get("integrity_check") or "UNKNOWN",
            "path": backup.get("backup_path"),
            "bytes": backup.get("bytes"),
            "sha256_status": "VERIFIED" if backup_complete else "UNKNOWN",
        },
        "scheduler": {
            "state": _state(scheduler.get("state") or "WAITING", "scheduler", diagnostics),
            "cycle": cycle_label,
            "stage": scheduler.get("stage") or "unknown",
            "estimated_remaining": scheduler.get("estimated_remaining"),
        },
        "alerts": list(bundle.get("alerts") or []) + [
            {"severity": "CRITICAL" if "EXECUTION" in code or "CONTRADICTION" in code else "WARNING", "code": code, "message": code.replace("_", " ").title()}
            for code in diagnostics
        ],
        "reports": reports,
        "workstreams": workstreams,
    }
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return {
        "phase": "UI-OBS-2",
        "mode": "LOCAL_READ_ONLY_CLOUD_STATUS_ADAPTER_SHADOW",
        "cloud_access": False,
        "database_access": False,
        "database_writes": 0,
        "execution_changed": False,
        "adapter_passed": not diagnostics,
        "diagnostics": diagnostics,
        "snapshot": snapshot,
        "ui_compatibility": build_progress_dashboard_from_payload(snapshot),
        "deterministic_digest": hashlib.sha256(canonical).hexdigest(),
    }


def build_progress_dashboard_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Exercise UI-OBS-1 normalization without writing a temporary status file."""
    # The adapter already emits the UI-OBS-1 contract; these are its critical invariants.
    return {
        "read_only": True,
        "valid_process_state": payload.get("active_process", {}).get("state") in VALID_STATES,
        "execution_disabled": payload.get("execution_enabled") is False,
        "required_workstreams_present": all(
            name in {item.get("name") for item in payload.get("workstreams", [])}
            for name in REQUIRED_WORKSTREAMS
        ),
    }


def write_cloud_status_adapter_preview(bundle_path: Path, output_dir: Path) -> Path:
    report = adapt_cloud_status_bundle(json.loads(bundle_path.read_text(encoding="utf-8")))
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ui_obs2_cloud_status_adapter_shadow.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
