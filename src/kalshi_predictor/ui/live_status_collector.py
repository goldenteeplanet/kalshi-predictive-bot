from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.ui.progress_history import history_path_for, record_progress_snapshot
from kalshi_predictor.ui.workstream_evidence import discover_workstream_evidence

CommandRunner = Callable[[list[str], int], str]


def _run(command: list[str], timeout: int) -> str:
    if command and command[0] == "kalshi-bot":
        command = [str(Path(sys.executable).with_name("kalshi-bot")), *command[1:]]
    completed = subprocess.run(  # noqa: S603 - fixed, operator-owned commands only.
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "EXECUTION_ENABLED": "false", "UI_READ_ONLY": "true"},
    )
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}): {command[0]}")
    return completed.stdout


def _boolean(text: str, pattern: str, default: bool = False) -> bool:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return default if match is None else match.group(1).lower() in {"yes", "true", "clear"}


def _field(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _systemd_show(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _latest_backup(root: Path) -> dict[str, Any]:
    candidates = sorted(
        root.glob("**/*.backup.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    for path in candidates[:20]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sha = payload.get("sha256") or payload.get("sha256_digest")
        integrity = payload.get("integrity_check") or payload.get("integrity")
        backup_path = payload.get("backup_path") or payload.get("path")
        if sha and backup_path:
            return {
                "state": "PASSED" if str(integrity).lower() == "ok" else "FAILED",
                "path": str(backup_path),
                "integrity": integrity,
                "sha256": sha,
                "sha256_status": "VERIFIED" if str(integrity).lower() == "ok" else "UNVERIFIED",
                "metadata_path": str(path),
            }
    return {"state": "WAITING", "integrity": "UNKNOWN", "diagnostic": "NO_VERIFIED_BACKUP_METADATA"}


def storage_status(paths: dict[str, Path]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    status: dict[str, Any] = {}
    alerts: list[dict[str, str]] = []
    for name, path in paths.items():
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            status[name] = {"path": str(path), "state": "UNKNOWN"}
            alerts.append(
                {
                    "severity": "WARNING",
                    "code": f"STORAGE_{name.upper()}_UNKNOWN",
                    "message": f"Storage usage is unavailable for {path}.",
                }
            )
            continue
        free_percent = round(usage.free / usage.total * 100, 2) if usage.total else 0.0
        state = "CRITICAL" if free_percent < 10 else "WARNING" if free_percent < 20 else "PASSED"
        status[name] = {
            "path": str(path),
            "state": state,
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "free_percent": free_percent,
        }
        if state != "PASSED":
            alerts.append(
                {
                    "severity": state,
                    "code": f"STORAGE_{name.upper()}_{state}",
                    "message": f"{path} has {free_percent:.2f}% free space.",
                }
            )
    return status, alerts


def collect_live_snapshot(
    *,
    runner: CommandRunner = _run,
    backup_root: Path = Path("/mnt/kalshi-backup"),
    service_name: str = "kalshi-r5-bounded.service",
    timer_name: str = "kalshi-r5-bounded.timer",
    collector_timer_name: str = "kalshi-ui-status-collector.timer",
    legacy_service_name: str = "kalshi-r5-watcher.service",
    roadmap_path: Path | None = None,
    r5_certification_path: Path | None = None,
    timeout_seconds: int = 10,
    poll_interval_seconds: int = 30,
    reports_root: Path = Path("reports"),
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    diagnostics: list[str] = []
    try:
        writer_text = runner(["kalshi-bot", "db-writer-monitor"], timeout_seconds)
    except Exception as exc:  # fail closed
        writer_text = ""
        diagnostics.append(f"WRITER_SOURCE_FAILURE:{type(exc).__name__}")
    try:
        locks_text = runner(["kalshi-bot", "db-locks"], timeout_seconds)
    except Exception as exc:
        locks_text = ""
        diagnostics.append(f"LOCK_SOURCE_FAILURE:{type(exc).__name__}")
    try:
        service = _systemd_show(
            runner(
                [
                    "systemctl",
                    "show",
                    service_name,
                    "-p",
                    "ActiveState",
                    "-p",
                    "SubState",
                    "-p",
                    "ExecMainPID",
                    "-p",
                    "ActiveEnterTimestamp",
                    "-p",
                    "Result",
                    "-p",
                    "MemoryCurrent",
                    "-p",
                    "MemoryPeak",
                    "-p",
                    "ExecMainStartTimestamp",
                    "-p",
                    "ExecMainExitTimestamp",
                ],
                timeout_seconds,
            )
        )
    except Exception as exc:
        service = {}
        diagnostics.append(f"SCHEDULER_SOURCE_FAILURE:{type(exc).__name__}")
    try:
        timer = _systemd_show(
            runner(
                [
                    "systemctl",
                    "show",
                    timer_name,
                    "-p",
                    "ActiveState",
                    "-p",
                    "SubState",
                    "-p",
                    "UnitFileState",
                    "-p",
                    "NextElapseUSecRealtime",
                    "-p",
                    "NextElapseUSecMonotonic",
                    "-p",
                    "LastTriggerUSec",
                ],
                timeout_seconds,
            )
        )
    except Exception as exc:
        timer = {}
        diagnostics.append(f"TIMER_SOURCE_FAILURE:{type(exc).__name__}")
    try:
        collector_timer = _systemd_show(
            runner(
                [
                    "systemctl",
                    "show",
                    collector_timer_name,
                    "-p",
                    "ActiveState",
                    "-p",
                    "SubState",
                    "-p",
                    "UnitFileState",
                    "-p",
                    "NextElapseUSecRealtime",
                    "-p",
                    "NextElapseUSecMonotonic",
                    "-p",
                    "LastTriggerUSec",
                ],
                timeout_seconds,
            )
        )
    except Exception as exc:
        collector_timer = {}
        diagnostics.append(f"COLLECTOR_TIMER_SOURCE_FAILURE:{type(exc).__name__}")
    try:
        legacy = _systemd_show(
            runner(
                [
                    "systemctl",
                    "show",
                    legacy_service_name,
                    "-p",
                    "ActiveState",
                    "-p",
                    "UnitFileState",
                ],
                timeout_seconds,
            )
        )
    except Exception as exc:
        legacy = {}
        diagnostics.append(f"LEGACY_SOURCE_FAILURE:{type(exc).__name__}")

    safe = _boolean(writer_text, r"Safe to start another write job:\s*(yes|no)") and not diagnostics
    writer_pid_text = _field(writer_text, r"Current writer PID:\s*(.+)$")
    writer_pid = int(writer_pid_text) if writer_pid_text and writer_pid_text.isdigit() else None
    locks_clear = (
        "diagnostics: CLEAR" in locks_text or "Open DB holders: none visible" in locks_text
    )
    readers_only = "diagnostics: OPEN_READERS" in locks_text and safe and writer_pid is None
    writer_clear = safe and writer_pid is None and (locks_clear or readers_only)
    pid = int(service.get("ExecMainPID") or 0)
    running = service.get("ActiveState") == "active" and pid > 0
    next_run = _timer_next_run(timer, service_running=running)
    collector_next_run = _timer_next_run(collector_timer, service_running=True)
    process_state = "RUNNING" if running else "WAITING"
    if diagnostics:
        process_state = "BLOCKED"
    storage, storage_alerts = storage_status(
        {
            "project": Path.cwd(),
            "backup": backup_root,
        }
    )
    evidence = discover_workstream_evidence(reports_root)
    roadmap = _load_json(roadmap_path)
    certification = _load_json(r5_certification_path)
    phases = roadmap.get("phases") if isinstance(roadmap.get("phases"), list) else []
    cert_status = certification.get("status")
    rollback_verified = (certification.get("gates") or {}).get("rollback_hash_verified") is True
    rollback_path = (certification.get("rollback") or {}).get("path")
    reports = list(evidence["reports"])
    if certification and not any(
        str(row.get("phase", "")).startswith("R5-RECOVERY-9") for row in reports
    ):
        reports.insert(
            0,
            {
                "phase": "R5-RECOVERY-9",
                "state": cert_status or "BLOCKED",
                "path": str(r5_certification_path),
                "verified": rollback_verified,
            },
        )
    return {
        "schema_version": "ui-obs-live/v1",
        "generated_at": now,
        "execution_enabled": False,
        "paper_enabled": False,
        "active_process": {
            "name": service_name,
            "pid": pid or None,
            "state": process_state,
            "stage": service.get("SubState") or "unknown",
            "runtime": "unknown",
            "estimated_remaining": "unknown",
            "completion_evidence": [],
        },
        "writer": {
            "state": "PASSED" if writer_clear else "BLOCKED",
            "safe_to_start_write": safe,
            "lock_status": "CLEAR"
            if locks_clear
            else "READERS_PRESENT"
            if readers_only
            else "UNKNOWN_OR_BUSY",
            "readers_present": readers_only,
            "current_writer_pid": writer_pid,
            "pid": writer_pid,
        },
        "backup": _latest_backup(backup_root),
        "scheduler": {
            "state": "RUNNING" if running else "WAITING",
            "cycle": "unknown",
            "service": service_name,
            "timer": timer_name,
            "next_run": next_run["value"],
            "next_run_state": next_run["state"],
            "next_run_basis": next_run["basis"],
            "schedule_state": next_run["state"],
            "current_cycle": None,
            "runtime_seconds": _runtime_seconds(service, now),
            "memory_current_bytes": _integer(service.get("MemoryCurrent")),
            "memory_peak_bytes": _integer(service.get("MemoryPeak")),
            "heartbeat": {},
            "last_result": service.get("Result", "unknown"),
            "result": service.get("Result", "unknown"),
            "legacy_watcher_enabled": legacy.get("UnitFileState") == "enabled",
            "legacy_watcher_active": legacy.get("ActiveState") == "active",
        },
        "phase_roadmap": phases,
        "r5_recovery9_certification": {
            "status": cert_status or "UNREPORTED",
            "rollback_verified": rollback_verified,
            "rollback_path": rollback_path,
        },
        "prov14b": {
            "state": next(
                (str(row.get("status")) for row in phases if row.get("phase") == "PROV-14B Resume"),
                "QUEUED",
            ),
            "reason": "Guarded backup-first attribution certification lane.",
        },
        "reports": reports,
        "alerts": [
            {"severity": "WARNING", "code": code.split(":", 1)[0], "message": code}
            for code in diagnostics
        ]
        + storage_alerts,
        "workstreams": evidence["workstreams"],
        "storage": storage,
        "collector": {
            "read_only": True,
            "database_writes": 0,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "timer": {
                "name": collector_timer_name,
                "active": collector_timer.get("ActiveState") == "active",
                "enabled": collector_timer.get("UnitFileState") == "enabled",
                "next_run": collector_next_run["value"],
                "next_run_state": collector_next_run["state"],
                "next_run_basis": collector_next_run["basis"],
                "last_trigger": collector_timer.get("LastTriggerUSec") or None,
            },
            "evidence_diagnostics": evidence["diagnostics"],
        },
    }


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _integer(value: Any) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _runtime_seconds(service: dict[str, str], now: str) -> int | None:
    if service.get("ActiveState") != "active":
        return None
    started = service.get("ExecMainStartTimestamp") or service.get("ActiveEnterTimestamp")
    try:
        start_time = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if start_time.tzinfo is None:
        return None
    return max(0, int((current - start_time.astimezone(UTC)).total_seconds()))


def _timer_next_run(timer: dict[str, str], *, service_running: bool) -> dict[str, Any]:
    realtime = str(timer.get("NextElapseUSecRealtime") or "").strip()
    if realtime and realtime.lower() not in {"n/a", "infinity", "0"}:
        return {
            "value": realtime,
            "state": "EXACT",
            "basis": "SYSTEMD_NEXT_ELAPSE_REALTIME",
        }
    if timer.get("ActiveState") == "inactive":
        return {
            "value": None,
            "state": "INACTIVE_NO_SCHEDULE",
            "basis": "SYSTEMD_TIMER_INACTIVE",
        }
    timer_waiting = timer.get("ActiveState") == "active" and timer.get("SubState") in {
        "waiting",
        "running",
        "elapsed",
    }
    if timer_waiting and service_running:
        return {
            "value": None,
            "state": "PENDING_SERVICE_EXIT",
            "basis": "ON_UNIT_ACTIVE_SEC_SCHEDULES_AFTER_COLLECTOR_EXIT",
        }
    return {"value": None, "state": "UNAVAILABLE", "basis": "SYSTEMD_DID_NOT_REPORT_NEXT_ELAPSE"}


def publish_live_snapshot(snapshot: dict[str, Any], destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    serialized = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    temporary.write_text(serialized, encoding="utf-8")
    os.replace(temporary, destination)
    history = record_progress_snapshot(snapshot, history_path_for(destination))
    return {
        "destination": str(destination),
        "sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "history_entries": len(history["entries"]),
        "published": True,
    }
