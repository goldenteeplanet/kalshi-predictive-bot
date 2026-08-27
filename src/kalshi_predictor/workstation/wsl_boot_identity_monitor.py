from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

MONITOR_SCHEMA_VERSION = "phase4hb-wsl-boot-identity-monitor-v1"
MonitorStatus = Literal["STABLE", "CHANGED", "STALE", "DEGRADED"]


class WslBootIdentityMonitorError(ValueError):
    """Stable fail-closed WSL boot-identity monitor error."""


@dataclass(frozen=True)
class WslBootIdentityObservation:
    sequence: int
    observed_at_epoch_seconds: int
    boot_identity: str
    complete: bool
    source_identity_hash: str
    evidence_age_seconds: int
    observation_hash: str


@dataclass(frozen=True)
class WslBootIdentityMonitorResult:
    status: MonitorStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    observation_count: int
    transition_count: int
    initial_boot_identity_hash: str
    current_boot_identity_hash: str
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    observations_hash: str
    result_hash: str
    read_only: bool = True
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_wsl_boot_identity_observation(
    *,
    sequence: int,
    observed_at_epoch_seconds: int,
    boot_identity: str,
    complete: bool,
    source_identity_hash: str,
    evidence_age_seconds: int,
) -> WslBootIdentityObservation:
    unsigned = {
        "sequence": sequence,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "boot_identity": boot_identity,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_observation_fields(unsigned)
    return WslBootIdentityObservation(**unsigned, observation_hash=_hash(unsigned))


def monitor_wsl_boot_identity(
    observations: Sequence[Any],
    *,
    max_observations: int = 128,
    max_evidence_age_seconds: int = 120,
) -> WslBootIdentityMonitorResult:
    if isinstance(max_observations, bool) or not isinstance(max_observations, int) or max_observations <= 0:
        raise WslBootIdentityMonitorError("MONITOR_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise WslBootIdentityMonitorError("MONITOR_BOUND_INVALID")
    if not observations:
        raise WslBootIdentityMonitorError("OBSERVATIONS_EMPTY")
    if len(observations) > max_observations:
        raise WslBootIdentityMonitorError("OBSERVATION_BOUND_EXCEEDED")

    validated = [_validated_observation(item) for item in observations]
    if len({item.sequence for item in validated}) != len(validated):
        raise WslBootIdentityMonitorError("OBSERVATION_SEQUENCE_DUPLICATE")
    if len({item.source_identity_hash for item in validated}) != 1:
        raise WslBootIdentityMonitorError("OBSERVATION_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: item.sequence)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.sequence != previous.sequence + 1:
            raise WslBootIdentityMonitorError("OBSERVATION_SEQUENCE_GAP")
        if current.observed_at_epoch_seconds <= previous.observed_at_epoch_seconds:
            raise WslBootIdentityMonitorError("OBSERVATION_TIME_NOT_MONOTONIC")

    transition_count = sum(
        current.boot_identity != previous.boot_identity
        for previous, current in zip(ordered, ordered[1:], strict=False)
    )
    observed_age = max(item.evidence_age_seconds for item in ordered)
    incomplete = [item.sequence for item in ordered if not item.complete]
    if observed_age > max_evidence_age_seconds:
        status: MonitorStatus = "STALE"
        reasons = ["BOOT_IDENTITY_EVIDENCE_STALE"]
    elif incomplete:
        status = "DEGRADED"
        reasons = [f"OBSERVATION_INCOMPLETE:{sequence}" for sequence in incomplete]
    elif transition_count:
        status = "CHANGED"
        reasons = ["WSL_BOOT_IDENTITY_CHANGED"]
    else:
        status = "STABLE"
        reasons = []

    observations_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": ordered[0].source_identity_hash,
        "observation_count": len(ordered),
        "transition_count": transition_count,
        "initial_boot_identity_hash": _hash(ordered[0].boot_identity),
        "current_boot_identity_hash": _hash(ordered[-1].boot_identity),
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "observations_hash": observations_hash,
        "read_only": True,
        "alert_required": status != "STABLE",
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return WslBootIdentityMonitorResult(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=ordered[0].source_identity_hash,
        observation_count=len(ordered),
        transition_count=transition_count,
        initial_boot_identity_hash=unsigned["initial_boot_identity_hash"],
        current_boot_identity_hash=unsigned["current_boot_identity_hash"],
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        observations_hash=observations_hash,
        result_hash=_hash(unsigned),
        alert_required=unsigned["alert_required"],
    )


def validate_wsl_boot_identity_monitor_result(result: Any) -> None:
    if not isinstance(result, WslBootIdentityMonitorResult):
        raise WslBootIdentityMonitorError("MONITOR_RESULT_TYPE_INVALID")
    if result.read_only is not True or any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    ):
        raise WslBootIdentityMonitorError("MONITOR_SAFETY_BOUNDARY_INVALID")
    if (result.status == "STABLE") != (not result.reasons and not result.alert_required):
        raise WslBootIdentityMonitorError("MONITOR_STATUS_INVALID")
    if result.status != "STABLE" and (not result.reasons or not result.alert_required):
        raise WslBootIdentityMonitorError("MONITOR_STATUS_INVALID")
    unsigned = asdict(result)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = MONITOR_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if result.result_hash != _hash(unsigned):
        raise WslBootIdentityMonitorError("MONITOR_RESULT_HASH_MISMATCH")


def _validated_observation(value: Any) -> WslBootIdentityObservation:
    if not isinstance(value, WslBootIdentityObservation):
        raise WslBootIdentityMonitorError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise WslBootIdentityMonitorError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    for key in ("boot_identity", "source_identity_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise WslBootIdentityMonitorError("OBSERVATION_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise WslBootIdentityMonitorError("OBSERVATION_FIELD_INVALID")
    for key in ("sequence", "observed_at_epoch_seconds", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WslBootIdentityMonitorError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
