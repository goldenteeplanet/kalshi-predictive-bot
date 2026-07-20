from __future__ import annotations

from typing import Any

STAGE_ORDER = ("backup_complete", "quick_check", "sha256", "integrity_check", "certified")
VALID_STATES = {"RUNNING", "WAITING", "BLOCKED", "PASSED", "FAILED"}


def normalize_backup_verification(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    diagnostics: list[str] = []
    stage = str(raw.get("stage") or "waiting")
    state = str(raw.get("state") or "WAITING").upper()
    if state not in VALID_STATES:
        state = "BLOCKED"
        diagnostics.append("BACKUP_VERIFICATION_STATE_INVALID")
    elapsed = _nonnegative(raw.get("elapsed_seconds"))
    progress = _bounded_percent(raw.get("progress_percent_lower_bound"))
    eta = _nonnegative(raw.get("estimated_remaining_seconds"))
    io_advanced = raw.get("io_advanced")
    stale = raw.get("stale") is True
    integrity = str(raw.get("integrity_status") or "PENDING").upper()
    sha = str(raw.get("sha256_status") or "PENDING").upper()
    if state == "PASSED" and (integrity != "OK" or sha != "VERIFIED"):
        state = "BLOCKED"
        diagnostics.append("BACKUP_SUCCESS_WITHOUT_VERIFICATION")
    if stale:
        diagnostics.append("BACKUP_VERIFICATION_IO_STALE")
    eta_confidence = "UNAVAILABLE"
    if eta is not None and io_advanced is True and not stale:
        eta_confidence = "LOWER_BOUND_ONLY"
    stages = []
    current_index = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
    for index, name in enumerate(STAGE_ORDER):
        if state == "FAILED" and index == current_index:
            stage_state = "FAILED"
        elif index < current_index or (name == "certified" and state == "PASSED"):
            stage_state = "PASSED"
        elif index == current_index:
            stage_state = state
        else:
            stage_state = "WAITING"
        stages.append({"name": name, "label": name.replace("_", " ").title(), "state": stage_state})
    return {
        "state": state,
        "stage": stage,
        "pid": raw.get("pid"),
        "elapsed_seconds": elapsed,
        "elapsed_label": _duration(elapsed),
        "database_bytes": raw.get("database_bytes"),
        "read_bytes": raw.get("read_bytes"),
        "progress_percent_lower_bound": progress,
        "estimated_remaining_seconds": eta,
        "estimated_remaining_label": _duration(eta) if eta is not None else "not calculable",
        "eta_confidence": eta_confidence,
        "io_advanced": io_advanced,
        "stale": stale,
        "integrity_status": integrity,
        "sha256_status": sha,
        "path": raw.get("path"),
        "safe_to_interrupt": False,
        "deployment_blocked": not (state == "PASSED" and integrity == "OK" and sha == "VERIFIED"),
        "stages": stages,
    }, diagnostics


def _nonnegative(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _bounded_percent(value: Any) -> float | None:
    parsed = _nonnegative(value)
    return min(100.0, parsed) if parsed is not None else None


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
