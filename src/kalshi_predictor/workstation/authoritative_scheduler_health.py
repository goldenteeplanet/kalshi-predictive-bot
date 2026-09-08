from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

PROBE_SCHEMA_VERSION = "phase4hf-authoritative-scheduler-health-probe-v1"
AUTHORITATIVE_UNIT = "kalshi-fixed-rate-refresh.service"
SchedulerStatus = Literal["HEALTHY", "UNHEALTHY", "STALE", "INCOMPLETE", "IDENTITY_MISMATCH"]


class AuthoritativeSchedulerHealthError(ValueError):
    """Stable fail-closed authoritative scheduler health evidence error."""


@dataclass(frozen=True)
class SchedulerHealthObservation:
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    duration_milliseconds: int
    unit_name: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int | None
    observed_writer_count: int | None
    complete: bool
    probe_name: str
    source_identity_hash: str
    output_hash: str
    observation_hash: str


@dataclass(frozen=True)
class SchedulerHealthEvidence:
    status: SchedulerStatus
    reasons: tuple[str, ...]
    unit_name: str
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    duration_milliseconds: int
    max_duration_milliseconds: int
    main_pid: int | None
    observed_writer_count: int | None
    probe_name: str
    source_identity_hash: str
    output_hash: str
    observation_hash: str
    evidence_hash: str
    read_only: bool = True
    writer_exclusive: bool = False
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_scheduler_health_observation(
    *,
    observed_at_epoch_seconds: int,
    evidence_age_seconds: int,
    duration_milliseconds: int,
    unit_name: str,
    load_state: str,
    active_state: str,
    sub_state: str,
    main_pid: int | None,
    observed_writer_count: int | None,
    complete: bool,
    probe_name: str,
    source_identity_hash: str,
    output: str,
    max_output_characters: int = 4096,
) -> SchedulerHealthObservation:
    if not isinstance(output, str):
        raise AuthoritativeSchedulerHealthError("PROBE_OUTPUT_INVALID")
    if (
        isinstance(max_output_characters, bool)
        or not isinstance(max_output_characters, int)
        or max_output_characters <= 0
    ):
        raise AuthoritativeSchedulerHealthError("PROBE_OUTPUT_BOUND_INVALID")
    if len(output) > max_output_characters:
        raise AuthoritativeSchedulerHealthError("PROBE_OUTPUT_BOUND_EXCEEDED")
    unsigned = {
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "evidence_age_seconds": evidence_age_seconds,
        "duration_milliseconds": duration_milliseconds,
        "unit_name": unit_name,
        "load_state": load_state,
        "active_state": active_state,
        "sub_state": sub_state,
        "main_pid": main_pid,
        "observed_writer_count": observed_writer_count,
        "complete": complete,
        "probe_name": probe_name,
        "source_identity_hash": source_identity_hash,
        "output_hash": _hash(output),
    }
    _validate_observation_fields(unsigned)
    return SchedulerHealthObservation(**unsigned, observation_hash=_hash(unsigned))


def probe_authoritative_scheduler_health(
    observation: Any,
    *,
    max_evidence_age_seconds: int = 120,
    max_duration_milliseconds: int = 5_000,
) -> SchedulerHealthEvidence:
    for bound in (max_evidence_age_seconds, max_duration_milliseconds):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise AuthoritativeSchedulerHealthError("PROBE_BOUND_INVALID")
    item = _validated_observation(observation)
    writer_exclusive = item.observed_writer_count == 1
    if item.evidence_age_seconds > max_evidence_age_seconds:
        status: SchedulerStatus = "STALE"
        reasons = ["SCHEDULER_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["SCHEDULER_PROBE_INCOMPLETE"]
    elif item.unit_name != AUTHORITATIVE_UNIT:
        status = "IDENTITY_MISMATCH"
        reasons = ["AUTHORITATIVE_UNIT_IDENTITY_MISMATCH"]
    else:
        failures = []
        if item.duration_milliseconds > max_duration_milliseconds:
            failures.append("SCHEDULER_PROBE_TIMEOUT")
        if item.load_state != "loaded":
            failures.append("SCHEDULER_NOT_LOADED")
        if item.active_state != "active":
            failures.append("SCHEDULER_NOT_ACTIVE")
        if item.sub_state != "running":
            failures.append("SCHEDULER_NOT_RUNNING")
        if item.main_pid is None or item.main_pid <= 0:
            failures.append("SCHEDULER_MAIN_PID_INVALID")
        if item.observed_writer_count is None:
            failures.append("WRITER_COUNT_MISSING")
        elif item.observed_writer_count != 1:
            failures.append("WRITER_EXCLUSIVITY_VIOLATION")
        status = "UNHEALTHY" if failures else "HEALTHY"
        reasons = sorted(failures)

    unsigned = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "unit_name": item.unit_name,
        "observed_at_epoch_seconds": item.observed_at_epoch_seconds,
        "evidence_age_seconds": item.evidence_age_seconds,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "duration_milliseconds": item.duration_milliseconds,
        "max_duration_milliseconds": max_duration_milliseconds,
        "main_pid": item.main_pid,
        "observed_writer_count": item.observed_writer_count,
        "probe_name": item.probe_name,
        "source_identity_hash": item.source_identity_hash,
        "output_hash": item.output_hash,
        "observation_hash": item.observation_hash,
        "read_only": True,
        "writer_exclusive": writer_exclusive,
        "alert_required": status != "HEALTHY",
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return SchedulerHealthEvidence(
        status=status,
        reasons=tuple(reasons),
        unit_name=item.unit_name,
        observed_at_epoch_seconds=item.observed_at_epoch_seconds,
        evidence_age_seconds=item.evidence_age_seconds,
        max_evidence_age_seconds=max_evidence_age_seconds,
        duration_milliseconds=item.duration_milliseconds,
        max_duration_milliseconds=max_duration_milliseconds,
        main_pid=item.main_pid,
        observed_writer_count=item.observed_writer_count,
        probe_name=item.probe_name,
        source_identity_hash=item.source_identity_hash,
        output_hash=item.output_hash,
        observation_hash=item.observation_hash,
        evidence_hash=_hash(unsigned),
        writer_exclusive=writer_exclusive,
        alert_required=unsigned["alert_required"],
    )


def validate_scheduler_health_evidence(evidence: Any) -> None:
    if not isinstance(evidence, SchedulerHealthEvidence):
        raise AuthoritativeSchedulerHealthError("EVIDENCE_TYPE_INVALID")
    if evidence.read_only is not True or any(
        (
            evidence.recovery_authorized,
            evidence.service_control_authorized,
            evidence.host_restart_authorized,
            evidence.execution_authorized,
        )
    ):
        raise AuthoritativeSchedulerHealthError("EVIDENCE_SAFETY_BOUNDARY_INVALID")
    healthy = not evidence.reasons and not evidence.alert_required and evidence.writer_exclusive
    if (evidence.status == "HEALTHY") != healthy:
        raise AuthoritativeSchedulerHealthError("EVIDENCE_STATUS_INVALID")
    unsigned = asdict(evidence)
    unsigned.pop("evidence_hash")
    unsigned["schema_version"] = PROBE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if evidence.evidence_hash != _hash(unsigned):
        raise AuthoritativeSchedulerHealthError("EVIDENCE_HASH_MISMATCH")


def _validated_observation(value: Any) -> SchedulerHealthObservation:
    if not isinstance(value, SchedulerHealthObservation):
        raise AuthoritativeSchedulerHealthError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise AuthoritativeSchedulerHealthError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    if not isinstance(payload["complete"], bool):
        raise AuthoritativeSchedulerHealthError("OBSERVATION_FIELD_INVALID")
    for key in (
        "unit_name",
        "load_state",
        "active_state",
        "sub_state",
        "probe_name",
        "source_identity_hash",
        "output_hash",
    ):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise AuthoritativeSchedulerHealthError("OBSERVATION_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "evidence_age_seconds", "duration_milliseconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuthoritativeSchedulerHealthError("OBSERVATION_FIELD_INVALID")
    for key in ("main_pid", "observed_writer_count"):
        value = payload[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise AuthoritativeSchedulerHealthError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
