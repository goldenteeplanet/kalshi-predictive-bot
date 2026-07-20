from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

PHASE_STATES = {"RUNNING", "WAITING", "BLOCKED", "PASSED", "FAILED", "PASSED_LOCAL_PREVIEW", "PASSED_CLOSED", "QUEUED"}


def build_live_roadmap_status(payload: Mapping[str, Any], *, reference_time: datetime) -> dict[str, Any]:
    raw_phases = payload.get("phase_roadmap", [])
    phases = []
    diagnostics: list[str] = []
    for item in raw_phases[:20] if isinstance(raw_phases, list) else []:
        if not isinstance(item, Mapping):
            continue
        state = str(item.get("status") or "BLOCKED").upper()
        if state not in PHASE_STATES:
            diagnostics.append(f"PHASE_STATE_INVALID:{item.get('number')}")
            state = "BLOCKED"
        phases.append({
            "number": int(item.get("number") or 0),
            "phase": str(item.get("phase") or "UNREPORTED"),
            "status": state,
            "evidence": str(item.get("evidence") or "No evidence reported"),
        })
    if len(phases) != 20:
        diagnostics.append("PHASE_ROADMAP_INCOMPLETE")

    scheduler = dict(payload.get("scheduler") or {})
    heartbeat = dict(scheduler.get("heartbeat") or {})
    heartbeat_at = _time(heartbeat.get("at"))
    heartbeat_age = None if heartbeat_at is None else max(0, int((reference_time - heartbeat_at).total_seconds()))
    heartbeat_interval = int(heartbeat.get("interval_seconds") or 15)
    heartbeat_stale = heartbeat_age is None or heartbeat_age > max(30, heartbeat_interval * 2)
    if scheduler.get("state") == "RUNNING" and heartbeat_stale:
        diagnostics.append("BOUNDED_CYCLE_HEARTBEAT_STALE")
    if scheduler.get("last_result") == "failed":
        diagnostics.append("BOUNDED_CYCLE_FAILED")
    legacy_disabled = scheduler.get("legacy_watcher_enabled") is False and scheduler.get("legacy_watcher_active") is False
    if not legacy_disabled:
        diagnostics.append("LEGACY_WATCHER_NOT_DISABLED")

    writer = dict(payload.get("writer") or {})
    if writer.get("lock_status") not in ("CLEAR", None):
        diagnostics.append("DATABASE_LOCK_NOT_CLEAR")
    execution_enabled = bool(payload.get("execution_enabled", False))
    if execution_enabled:
        diagnostics.append("EXECUTION_ENABLED_CRITICAL")
    certification = dict(payload.get("r5_recovery9_certification") or {})
    rollback_verified = certification.get("rollback_verified") is True
    if certification.get("status") != "PASSED" or not rollback_verified:
        diagnostics.append("R5_RECOVERY9_CERTIFICATION_INVALID")

    return {
        "read_only": True,
        "phase_count": len(phases),
        "phases": sorted(phases, key=lambda row: row["number"]),
        "scheduler": {
            "state": str(scheduler.get("state") or "WAITING"),
            "timer": str(scheduler.get("timer") or "UNREPORTED"),
            "next_run": scheduler.get("next_run"),
            "current_cycle": scheduler.get("current_cycle"),
            "runtime_seconds": scheduler.get("runtime_seconds"),
            "memory_current_bytes": scheduler.get("memory_current_bytes"),
            "memory_peak_bytes": scheduler.get("memory_peak_bytes"),
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_stale": heartbeat_stale,
            "legacy_watcher_disabled": legacy_disabled,
        },
        "writer": {
            "safe_to_start_write": writer.get("safe_to_start_write") is True,
            "lock_status": str(writer.get("lock_status") or "UNKNOWN"),
        },
        "execution_enabled": execution_enabled,
        "r5_recovery9": {
            "status": certification.get("status") or "UNREPORTED",
            "rollback_verified": rollback_verified,
            "rollback_path": certification.get("rollback_path"),
        },
        "prov14b": dict(payload.get("prov14b") or {"state": "QUEUED"}),
        "diagnostics": sorted(set(diagnostics)),
        "alerts": [
            {"severity": "CRITICAL" if code in {"EXECUTION_ENABLED_CRITICAL", "BOUNDED_CYCLE_FAILED"} else "WARNING", "code": code}
            for code in sorted(set(diagnostics))
        ],
    }


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None
