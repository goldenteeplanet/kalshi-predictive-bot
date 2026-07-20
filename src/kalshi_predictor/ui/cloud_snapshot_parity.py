from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


REQUIRED_WORKSTREAMS = {"pmb", "prov", "nyc_weather", "gh_liquidity", "readiness"}


def certify_cloud_snapshot_parity(
    snapshot: Mapping[str, Any],
    authoritative: Mapping[str, Any],
    *,
    reference_time: datetime,
    max_age_seconds: int = 120,
) -> dict[str, Any]:
    """Compare a captured UI snapshot with read-only authoritative runtime state."""
    failures: list[str] = []
    warnings: list[str] = []
    captured_at = _time(snapshot.get("generated_at"))
    age = None if captured_at is None else max(0, int((reference_time - captured_at).total_seconds()))
    if age is None:
        failures.append("CAPTURE_TIMESTAMP_INVALID")
    elif age > max_age_seconds:
        failures.append("CAPTURE_STALE")

    if snapshot.get("execution_enabled") is not False:
        failures.append("EXECUTION_NOT_CONFIRMED_DISABLED")
    if authoritative.get("execution_enabled") is not False:
        failures.append("AUTHORITATIVE_EXECUTION_NOT_DISABLED")

    writer = snapshot.get("writer") if isinstance(snapshot.get("writer"), Mapping) else {}
    if writer.get("lock_status") != authoritative.get("lock_status"):
        failures.append("WRITER_LOCK_PARITY_MISMATCH")
    if writer.get("safe_to_start_write") is not authoritative.get("safe_to_start_write"):
        failures.append("WRITER_CLEARANCE_PARITY_MISMATCH")

    scheduler = snapshot.get("scheduler") if isinstance(snapshot.get("scheduler"), Mapping) else {}
    if scheduler.get("service") != authoritative.get("bounded_service"):
        failures.append("SCHEDULER_SERVICE_STALE")
    if authoritative.get("legacy_enabled") is not False or authoritative.get("legacy_active") is not False:
        failures.append("LEGACY_WATCHER_NOT_DISABLED")
    if authoritative.get("bounded_timer_enabled") is not True:
        failures.append("BOUNDED_TIMER_NOT_ENABLED")
    if authoritative.get("bounded_timer_active") is False:
        warnings.append("BOUNDED_TIMER_INTENTIONALLY_ISOLATED")

    workstreams = snapshot.get("workstreams") if isinstance(snapshot.get("workstreams"), list) else []
    ids = {str(row.get("id")) for row in workstreams if isinstance(row, Mapping)}
    missing = sorted(REQUIRED_WORKSTREAMS - ids)
    if missing:
        failures.append("WORKSTREAMS_MISSING:" + ",".join(missing))

    phase_roadmap = snapshot.get("phase_roadmap")
    if not isinstance(phase_roadmap, list) or len(phase_roadmap) != 20:
        failures.append("TWENTY_PHASE_ROADMAP_MISSING")

    reports = snapshot.get("reports") if isinstance(snapshot.get("reports"), list) else []
    has_r5 = any(str(row.get("phase", "")).startswith("R5-RECOVERY-9") for row in reports if isinstance(row, Mapping))
    if not has_r5:
        failures.append("R5_RECOVERY9_EVIDENCE_MISSING")
    prov = next((row for row in workstreams if isinstance(row, Mapping) and row.get("id") == "prov"), {})
    prov14b = snapshot.get("prov14b") if isinstance(snapshot.get("prov14b"), Mapping) else {}
    explicit_prov_state = str(prov14b.get("state", "")).upper()
    legacy_prov_phase = str(prov.get("current_phase", "")).upper()
    if explicit_prov_state not in {"QUEUED", "WAITING", "RUNNING", "PASSED"} and legacy_prov_phase not in {"PROV-14B", "PROV-14"}:
        failures.append("PROV14B_STATUS_MISSING")

    return {
        "phase": "UI-OBS-5D",
        "mode": "CAPTURED_CLOUD_READ_ONLY_PARITY_AND_DEPLOYMENT_PREVIEW",
        "captured_at": snapshot.get("generated_at"),
        "capture_age_seconds": age,
        "max_capture_age_seconds": max_age_seconds,
        "parity_passed": not failures,
        "deployment_ready": not failures,
        "deployment_performed": False,
        "cloud_writes": 0,
        "database_writes": 0,
        "service_controls": 0,
        "execution_enabled": authoritative.get("execution_enabled"),
        "failures": sorted(set(failures)),
        "warnings": sorted(set(warnings)),
        "field_coverage": {
            "workstreams_reported": len(ids & REQUIRED_WORKSTREAMS),
            "workstreams_required": len(REQUIRED_WORKSTREAMS),
            "roadmap_phases_reported": len(phase_roadmap) if isinstance(phase_roadmap, list) else 0,
            "scheduler_service": scheduler.get("service"),
            "authoritative_scheduler_service": authoritative.get("bounded_service"),
        },
        "guarded_deployment_preview": {
            "requires_new_explicit_approval": True,
            "preconditions": [
                "fresh captured snapshot passes exact parity",
                "verified rollback evidence",
                "bounded polling and read-only collector",
                "execution remains disabled",
                "immediate rollback on failed smoke or safety gate",
            ],
        },
    }


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None
