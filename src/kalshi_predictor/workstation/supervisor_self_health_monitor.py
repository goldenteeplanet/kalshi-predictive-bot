from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

MONITOR_SCHEMA_VERSION = "phase4jy-supervisor-self-health-monitor-v1"
MAX_HEARTBEAT_AGE_SECONDS = 180
MAX_LOOP_DURATION_SECONDS = 60
MAX_CONSECUTIVE_ERRORS = 2
HealthStatus = Literal["HEALTHY", "DEGRADED", "FAILED", "INCOMPLETE", "TAMPERED"]


class SupervisorSelfHealthMonitorError(ValueError):
    """Stable fail-closed supervisor self-health monitor error."""


@dataclass(frozen=True)
class SupervisorSelfHealthEvidence:
    supervisor_instance_hash: str
    heartbeat_hash: str
    exclusion_lock_hash: str
    evaluated_at_epoch: int
    heartbeat_observed_at_epoch: int
    last_loop_duration_seconds: int
    consecutive_internal_errors: int
    lock_owned_by_instance: bool
    heartbeat_integrity_verified: bool
    evidence_complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class SupervisorSelfHealthDecision:
    status: HealthStatus
    reasons: tuple[str, ...]
    evidence_hash: str
    heartbeat_age_seconds: int
    last_loop_duration_seconds: int
    consecutive_internal_errors: int
    decision_hash: str
    read_only: bool = True
    supervisor_healthy: bool = False
    alert_required: bool = True
    self_restart_authorized: bool = False
    host_restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_supervisor_self_health_evidence(
    **fields: Any,
) -> SupervisorSelfHealthEvidence:
    _validate_fields(fields)
    return SupervisorSelfHealthEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_supervisor_self_health(evidence: Any) -> SupervisorSelfHealthDecision:
    item = _validated_evidence(evidence)
    future_heartbeat = item.heartbeat_observed_at_epoch > item.evaluated_at_epoch
    age = item.evaluated_at_epoch - item.heartbeat_observed_at_epoch if not future_heartbeat else 0
    if future_heartbeat:
        status: HealthStatus = "TAMPERED"
        reasons = ["SUPERVISOR_HEALTH_HEARTBEAT_FROM_FUTURE"]
    elif not item.evidence_complete or not item.heartbeat_integrity_verified:
        status = "INCOMPLETE"
        reasons = ["SUPERVISOR_HEALTH_EVIDENCE_UNTRUSTED"]
    else:
        failed = []
        degraded = []
        if not item.lock_owned_by_instance:
            failed.append("SUPERVISOR_HEALTH_LOCK_OWNERSHIP_LOST")
        if age > MAX_HEARTBEAT_AGE_SECONDS:
            failed.append("SUPERVISOR_HEALTH_HEARTBEAT_STALE")
        if item.consecutive_internal_errors > MAX_CONSECUTIVE_ERRORS:
            failed.append("SUPERVISOR_HEALTH_ERROR_LIMIT_EXCEEDED")
        elif item.consecutive_internal_errors:
            degraded.append("SUPERVISOR_HEALTH_INTERNAL_ERRORS")
        if item.last_loop_duration_seconds > MAX_LOOP_DURATION_SECONDS:
            degraded.append("SUPERVISOR_HEALTH_LOOP_SLOW")
        if failed:
            status = "FAILED"
            reasons = [*failed, *degraded]
        elif degraded:
            status = "DEGRADED"
            reasons = degraded
        else:
            status = "HEALTHY"
            reasons = []
    healthy = status == "HEALTHY"
    unsigned = {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "heartbeat_age_seconds": age,
        "last_loop_duration_seconds": item.last_loop_duration_seconds,
        "consecutive_internal_errors": item.consecutive_internal_errors,
        "read_only": True,
        "supervisor_healthy": healthy,
        "alert_required": not healthy,
        "self_restart_authorized": False,
        "host_restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return SupervisorSelfHealthDecision(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        heartbeat_age_seconds=age,
        last_loop_duration_seconds=item.last_loop_duration_seconds,
        consecutive_internal_errors=item.consecutive_internal_errors,
        decision_hash=_hash(unsigned),
        supervisor_healthy=healthy,
        alert_required=not healthy,
    )


def validate_supervisor_self_health_decision(value: Any) -> None:
    if not isinstance(value, SupervisorSelfHealthDecision):
        raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_DECISION_TYPE_INVALID")
    healthy = value.status == "HEALTHY"
    if (
        value.read_only is not True
        or value.supervisor_healthy != healthy
        or value.alert_required != (not healthy)
        or any(
            (
                value.self_restart_authorized,
                value.host_restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = MONITOR_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> SupervisorSelfHealthEvidence:
    if not isinstance(value, SupervisorSelfHealthEvidence):
        raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "supervisor_instance_hash",
        "heartbeat_hash",
        "exclusion_lock_hash",
        "evaluated_at_epoch",
        "heartbeat_observed_at_epoch",
        "last_loop_duration_seconds",
        "consecutive_internal_errors",
        "lock_owned_by_instance",
        "heartbeat_integrity_verified",
        "evidence_complete",
    }
    if set(fields) != required:
        raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_EVIDENCE_FIELD_INVALID")
    for key in ("supervisor_instance_hash", "heartbeat_hash", "exclusion_lock_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_EVIDENCE_FIELD_INVALID")
    for key in (
        "evaluated_at_epoch",
        "heartbeat_observed_at_epoch",
        "last_loop_duration_seconds",
        "consecutive_internal_errors",
    ):
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 0:
            raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_EVIDENCE_FIELD_INVALID")
    for key in ("lock_owned_by_instance", "heartbeat_integrity_verified", "evidence_complete"):
        if not isinstance(fields[key], bool):
            raise SupervisorSelfHealthMonitorError("SUPERVISOR_HEALTH_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
