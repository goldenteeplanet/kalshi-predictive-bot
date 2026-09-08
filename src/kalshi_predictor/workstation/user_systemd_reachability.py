from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

PROBE_SCHEMA_VERSION = "phase4he-user-systemd-reachability-probe-v1"
ReachabilityStatus = Literal["REACHABLE", "UNREACHABLE", "STALE", "INCOMPLETE"]


class UserSystemdReachabilityError(ValueError):
    """Stable fail-closed user-systemd reachability evidence error."""


@dataclass(frozen=True)
class UserSystemdProbeObservation:
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    duration_milliseconds: int
    exit_code: int | None
    manager_reachable: bool
    runtime_directory_present: bool
    dbus_session_present: bool
    complete: bool
    probe_name: str
    source_identity_hash: str
    output_hash: str
    observation_hash: str


@dataclass(frozen=True)
class UserSystemdReachabilityEvidence:
    status: ReachabilityStatus
    reasons: tuple[str, ...]
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    duration_milliseconds: int
    max_duration_milliseconds: int
    exit_code: int | None
    probe_name: str
    source_identity_hash: str
    output_hash: str
    observation_hash: str
    evidence_hash: str
    read_only: bool = True
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_user_systemd_probe_observation(
    *,
    observed_at_epoch_seconds: int,
    evidence_age_seconds: int,
    duration_milliseconds: int,
    exit_code: int | None,
    manager_reachable: bool,
    runtime_directory_present: bool,
    dbus_session_present: bool,
    complete: bool,
    probe_name: str,
    source_identity_hash: str,
    output: str,
    max_output_characters: int = 4096,
) -> UserSystemdProbeObservation:
    if not isinstance(output, str):
        raise UserSystemdReachabilityError("PROBE_OUTPUT_INVALID")
    if (
        isinstance(max_output_characters, bool)
        or not isinstance(max_output_characters, int)
        or max_output_characters <= 0
    ):
        raise UserSystemdReachabilityError("PROBE_OUTPUT_BOUND_INVALID")
    if len(output) > max_output_characters:
        raise UserSystemdReachabilityError("PROBE_OUTPUT_BOUND_EXCEEDED")
    unsigned = {
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "evidence_age_seconds": evidence_age_seconds,
        "duration_milliseconds": duration_milliseconds,
        "exit_code": exit_code,
        "manager_reachable": manager_reachable,
        "runtime_directory_present": runtime_directory_present,
        "dbus_session_present": dbus_session_present,
        "complete": complete,
        "probe_name": probe_name,
        "source_identity_hash": source_identity_hash,
        "output_hash": _hash(output),
    }
    _validate_observation_fields(unsigned)
    return UserSystemdProbeObservation(**unsigned, observation_hash=_hash(unsigned))


def probe_user_systemd_reachability(
    observation: Any,
    *,
    max_evidence_age_seconds: int = 120,
    max_duration_milliseconds: int = 5_000,
) -> UserSystemdReachabilityEvidence:
    for bound in (max_evidence_age_seconds, max_duration_milliseconds):
        if isinstance(bound, bool) or not isinstance(bound, int) or bound < 0:
            raise UserSystemdReachabilityError("PROBE_BOUND_INVALID")
    item = _validated_observation(observation)
    if item.evidence_age_seconds > max_evidence_age_seconds:
        status: ReachabilityStatus = "STALE"
        reasons = ["USER_SYSTEMD_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["USER_SYSTEMD_PROBE_INCOMPLETE"]
    else:
        failures = []
        if item.duration_milliseconds > max_duration_milliseconds:
            failures.append("USER_SYSTEMD_PROBE_TIMEOUT")
        if item.exit_code != 0:
            failures.append("USER_SYSTEMD_PROBE_EXIT_NONZERO")
        if not item.runtime_directory_present:
            failures.append("USER_RUNTIME_DIRECTORY_MISSING")
        if not item.dbus_session_present:
            failures.append("USER_DBUS_SESSION_MISSING")
        if not item.manager_reachable:
            failures.append("USER_SYSTEMD_MANAGER_UNREACHABLE")
        status = "UNREACHABLE" if failures else "REACHABLE"
        reasons = sorted(failures)

    unsigned = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "observed_at_epoch_seconds": item.observed_at_epoch_seconds,
        "evidence_age_seconds": item.evidence_age_seconds,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "duration_milliseconds": item.duration_milliseconds,
        "max_duration_milliseconds": max_duration_milliseconds,
        "exit_code": item.exit_code,
        "probe_name": item.probe_name,
        "source_identity_hash": item.source_identity_hash,
        "output_hash": item.output_hash,
        "observation_hash": item.observation_hash,
        "read_only": True,
        "alert_required": status != "REACHABLE",
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return UserSystemdReachabilityEvidence(
        status=status,
        reasons=tuple(reasons),
        observed_at_epoch_seconds=item.observed_at_epoch_seconds,
        evidence_age_seconds=item.evidence_age_seconds,
        max_evidence_age_seconds=max_evidence_age_seconds,
        duration_milliseconds=item.duration_milliseconds,
        max_duration_milliseconds=max_duration_milliseconds,
        exit_code=item.exit_code,
        probe_name=item.probe_name,
        source_identity_hash=item.source_identity_hash,
        output_hash=item.output_hash,
        observation_hash=item.observation_hash,
        evidence_hash=_hash(unsigned),
        alert_required=unsigned["alert_required"],
    )


def validate_user_systemd_reachability_evidence(evidence: Any) -> None:
    if not isinstance(evidence, UserSystemdReachabilityEvidence):
        raise UserSystemdReachabilityError("EVIDENCE_TYPE_INVALID")
    if evidence.read_only is not True or any(
        (
            evidence.recovery_authorized,
            evidence.service_control_authorized,
            evidence.host_restart_authorized,
            evidence.execution_authorized,
        )
    ):
        raise UserSystemdReachabilityError("EVIDENCE_SAFETY_BOUNDARY_INVALID")
    healthy = not evidence.reasons and not evidence.alert_required
    if (evidence.status == "REACHABLE") != healthy:
        raise UserSystemdReachabilityError("EVIDENCE_STATUS_INVALID")
    unsigned = asdict(evidence)
    unsigned.pop("evidence_hash")
    unsigned["schema_version"] = PROBE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if evidence.evidence_hash != _hash(unsigned):
        raise UserSystemdReachabilityError("EVIDENCE_HASH_MISMATCH")


def _validated_observation(value: Any) -> UserSystemdProbeObservation:
    if not isinstance(value, UserSystemdProbeObservation):
        raise UserSystemdReachabilityError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise UserSystemdReachabilityError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    for key in (
        "manager_reachable",
        "runtime_directory_present",
        "dbus_session_present",
        "complete",
    ):
        if not isinstance(payload[key], bool):
            raise UserSystemdReachabilityError("OBSERVATION_FIELD_INVALID")
    for key in ("probe_name", "source_identity_hash", "output_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise UserSystemdReachabilityError("OBSERVATION_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "evidence_age_seconds", "duration_milliseconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise UserSystemdReachabilityError("OBSERVATION_FIELD_INVALID")
    exit_code = payload["exit_code"]
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise UserSystemdReachabilityError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
