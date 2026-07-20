from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Mapping


VALID_STATES = {"RUNNING", "WAITING", "BLOCKED", "PASSED", "FAILED"}
MAX_ETA = timedelta(days=7)
STALE_AFTER_SECONDS = 300


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    total = int(seconds)
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, _ = divmod(total, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def normalize_process_progress(process: Mapping[str, Any], *, reference_time: datetime) -> tuple[dict[str, Any], list[str]]:
    diagnostics: list[str] = []
    state = str(process.get("state") or "WAITING").upper()
    if state not in VALID_STATES:
        state = "BLOCKED"
        diagnostics.append("PROCESS_STATE_INVALID")
    started_at = _time(process.get("started_at"))
    updated_at = _time(process.get("updated_at") or process.get("observed_at"))
    runtime_seconds = (reference_time - started_at).total_seconds() if started_at else None
    runtime = _duration(runtime_seconds) if started_at else str(process.get("runtime") or "unknown")
    runtime_source = "TIMESTAMPS" if started_at else ("REPORTED" if process.get("runtime") else "UNKNOWN")
    if started_at and started_at > reference_time:
        diagnostics.append("PROCESS_START_FROM_FUTURE")
        state = "BLOCKED"
        runtime, runtime_seconds = "unknown", None
    freshness_age = max(0, int((reference_time - updated_at).total_seconds())) if updated_at else None
    freshness = "STALE" if freshness_age is not None and freshness_age > STALE_AFTER_SECONDS else ("FRESH" if updated_at else "UNKNOWN")
    if state == "RUNNING" and freshness == "STALE":
        diagnostics.append("PROCESS_EVIDENCE_STALE")
        state = "BLOCKED"
    if state == "RUNNING" and not process.get("pid"):
        diagnostics.append("PROCESS_RUNNING_WITHOUT_PID")
        state = "BLOCKED"
    completed = process.get("completed_units")
    total = process.get("total_units")
    units_valid = isinstance(completed, int) and isinstance(total, int) and total > 0 and 0 <= completed <= total
    if (completed is not None or total is not None) and not units_valid:
        diagnostics.append("PROCESS_UNITS_INVALID")
        state = "BLOCKED"
    reported_percent = process.get("progress_percent")
    derived_percent = round(completed / total * 100, 2) if units_valid else None
    if derived_percent is not None and isinstance(reported_percent, (int, float)) and abs(float(reported_percent) - derived_percent) > 1:
        diagnostics.append("PROCESS_PROGRESS_CONTRADICTION")
        state = "BLOCKED"
    percent = derived_percent if derived_percent is not None else (round(float(reported_percent), 2) if isinstance(reported_percent, (int, float)) and 0 <= float(reported_percent) <= 100 else None)
    if reported_percent is not None and percent is None:
        diagnostics.append("PROCESS_PERCENT_INVALID")
        state = "BLOCKED"
    eta_seconds = None
    eta_reason = "INSUFFICIENT_EVIDENCE"
    if state == "RUNNING" and units_valid and runtime_seconds and completed and completed < total:
        estimate = runtime_seconds / completed * (total - completed)
        if estimate <= MAX_ETA.total_seconds():
            eta_seconds, eta_reason = int(estimate), "CALCULATED_FROM_THROUGHPUT"
        else:
            eta_reason = "ESTIMATE_EXCEEDS_BOUND"
    elif state == "PASSED":
        eta_seconds, eta_reason = 0, "COMPLETE"
    if state == "PASSED" and units_valid and completed != total:
        diagnostics.append("PROCESS_COMPLETION_CONTRADICTION")
        state, eta_seconds, eta_reason = "BLOCKED", None, "CONTRADICTORY_EVIDENCE"
    stage = " ".join(str(process.get("stage") or "unknown").split())
    return {
        "name": process.get("name") or "No certified active process",
        "pid": process.get("pid"),
        "runtime": runtime,
        "runtime_seconds": int(runtime_seconds) if runtime_seconds is not None else None,
        "runtime_source": runtime_source,
        "stage": stage,
        "state": state,
        "completed_units": completed if units_valid else None,
        "total_units": total if units_valid else None,
        "progress_percent": percent,
        "progress_source": "UNITS" if derived_percent is not None else ("REPORTED" if percent is not None else "UNKNOWN"),
        "estimated_remaining": _duration(eta_seconds) if eta_seconds is not None else "unknown",
        "estimated_remaining_seconds": eta_seconds,
        "eta_reason": eta_reason,
        "freshness": freshness,
        "freshness_age_seconds": freshness_age,
        "completion_evidence": process.get("completion_evidence"),
    }, diagnostics
