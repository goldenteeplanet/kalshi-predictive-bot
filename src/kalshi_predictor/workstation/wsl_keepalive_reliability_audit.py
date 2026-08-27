from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

AUDIT_SCHEMA_VERSION = "phase4ha-wsl-keepalive-reliability-audit-v1"
AuditStatus = Literal["HEALTHY", "DEGRADED", "STALE"]


class WslKeepaliveReliabilityAuditError(ValueError):
    """Stable fail-closed WSL keepalive audit error."""


@dataclass(frozen=True)
class WslKeepaliveObservation:
    sequence: int
    observed_at_epoch_seconds: int
    wsl_running: bool
    keepalive_present: bool
    user_systemd_reachable: bool
    authoritative_service_active: bool
    complete: bool
    source_identity_hash: str
    evidence_age_seconds: int
    observation_hash: str


@dataclass(frozen=True)
class WslKeepaliveReliabilityAudit:
    status: AuditStatus
    reasons: tuple[str, ...]
    source_identity_hash: str
    observation_count: int
    failure_count: int
    observed_max_gap_seconds: int
    max_gap_seconds: int
    observed_max_age_seconds: int
    max_evidence_age_seconds: int
    observations_hash: str
    audit_hash: str
    read_only: bool = True
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_wsl_keepalive_observation(
    *,
    sequence: int,
    observed_at_epoch_seconds: int,
    wsl_running: bool,
    keepalive_present: bool,
    user_systemd_reachable: bool,
    authoritative_service_active: bool,
    complete: bool,
    source_identity_hash: str,
    evidence_age_seconds: int,
) -> WslKeepaliveObservation:
    unsigned = {
        "sequence": sequence,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "wsl_running": wsl_running,
        "keepalive_present": keepalive_present,
        "user_systemd_reachable": user_systemd_reachable,
        "authoritative_service_active": authoritative_service_active,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
        "evidence_age_seconds": evidence_age_seconds,
    }
    _validate_observation_fields(unsigned)
    return WslKeepaliveObservation(**unsigned, observation_hash=_hash(unsigned))


def audit_wsl_keepalive_reliability(
    observations: Sequence[Any],
    *,
    max_observations: int = 128,
    max_gap_seconds: int = 60,
    max_evidence_age_seconds: int = 120,
) -> WslKeepaliveReliabilityAudit:
    for value in (max_observations, max_gap_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WslKeepaliveReliabilityAuditError("AUDIT_BOUND_INVALID")
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise WslKeepaliveReliabilityAuditError("AUDIT_BOUND_INVALID")
    if not observations:
        raise WslKeepaliveReliabilityAuditError("OBSERVATIONS_EMPTY")
    if len(observations) > max_observations:
        raise WslKeepaliveReliabilityAuditError("OBSERVATION_BOUND_EXCEEDED")

    validated = [_validated_observation(item) for item in observations]
    sequences = [item.sequence for item in validated]
    if len(set(sequences)) != len(sequences):
        raise WslKeepaliveReliabilityAuditError("OBSERVATION_SEQUENCE_DUPLICATE")
    identities = {item.source_identity_hash for item in validated}
    if len(identities) != 1:
        raise WslKeepaliveReliabilityAuditError("OBSERVATION_LINEAGE_MIXED")
    ordered = sorted(validated, key=lambda item: item.sequence)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current.sequence != previous.sequence + 1:
            raise WslKeepaliveReliabilityAuditError("OBSERVATION_SEQUENCE_GAP")
        if current.observed_at_epoch_seconds <= previous.observed_at_epoch_seconds:
            raise WslKeepaliveReliabilityAuditError("OBSERVATION_TIME_NOT_MONOTONIC")
    gaps = [
        current.observed_at_epoch_seconds - previous.observed_at_epoch_seconds
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ]
    observed_gap = max(gaps, default=0)
    observed_age = max(item.evidence_age_seconds for item in ordered)
    failures: list[str] = []
    if observed_gap > max_gap_seconds:
        failures.append("KEEPALIVE_GAP_EXCEEDED")
    for item in ordered:
        if not item.complete:
            failures.append(f"OBSERVATION_INCOMPLETE:{item.sequence}")
        if not item.wsl_running:
            failures.append(f"WSL_NOT_RUNNING:{item.sequence}")
        if not item.keepalive_present:
            failures.append(f"KEEPALIVE_MISSING:{item.sequence}")
        if not item.user_systemd_reachable:
            failures.append(f"USER_SYSTEMD_UNREACHABLE:{item.sequence}")
        if not item.authoritative_service_active:
            failures.append(f"AUTHORITATIVE_SERVICE_INACTIVE:{item.sequence}")

    if observed_age > max_evidence_age_seconds:
        status: AuditStatus = "STALE"
        reasons = ["KEEPALIVE_EVIDENCE_STALE"]
    elif failures:
        status = "DEGRADED"
        reasons = sorted(failures)
    else:
        status = "HEALTHY"
        reasons = []

    observations_hash = _hash([asdict(item) for item in ordered])
    unsigned = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "source_identity_hash": ordered[0].source_identity_hash,
        "observation_count": len(ordered),
        "failure_count": len(reasons) if status == "DEGRADED" else 0,
        "observed_max_gap_seconds": observed_gap,
        "max_gap_seconds": max_gap_seconds,
        "observed_max_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "observations_hash": observations_hash,
        "read_only": True,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return WslKeepaliveReliabilityAudit(
        status=status,
        reasons=tuple(reasons),
        source_identity_hash=ordered[0].source_identity_hash,
        observation_count=len(ordered),
        failure_count=unsigned["failure_count"],
        observed_max_gap_seconds=observed_gap,
        max_gap_seconds=max_gap_seconds,
        observed_max_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        observations_hash=observations_hash,
        audit_hash=_hash(unsigned),
    )


def validate_wsl_keepalive_reliability_audit(audit: Any) -> None:
    if not isinstance(audit, WslKeepaliveReliabilityAudit):
        raise WslKeepaliveReliabilityAuditError("AUDIT_RESULT_TYPE_INVALID")
    if audit.read_only is not True or any(
        (audit.recovery_authorized, audit.service_control_authorized, audit.execution_authorized)
    ):
        raise WslKeepaliveReliabilityAuditError("AUDIT_SAFETY_BOUNDARY_INVALID")
    if audit.status == "HEALTHY" and (audit.reasons or audit.failure_count):
        raise WslKeepaliveReliabilityAuditError("HEALTHY_STATE_INVALID")
    if audit.status == "DEGRADED" and (not audit.reasons or not audit.failure_count):
        raise WslKeepaliveReliabilityAuditError("DEGRADED_STATE_INVALID")
    if audit.status == "STALE" and not audit.reasons:
        raise WslKeepaliveReliabilityAuditError("STALE_REASONS_MISSING")
    unsigned = asdict(audit)
    unsigned.pop("audit_hash")
    unsigned["schema_version"] = AUDIT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if audit.audit_hash != _hash(unsigned):
        raise WslKeepaliveReliabilityAuditError("AUDIT_HASH_MISMATCH")


def _validated_observation(value: Any) -> WslKeepaliveObservation:
    if not isinstance(value, WslKeepaliveObservation):
        raise WslKeepaliveReliabilityAuditError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise WslKeepaliveReliabilityAuditError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    if not isinstance(payload["source_identity_hash"], str) or not payload["source_identity_hash"]:
        raise WslKeepaliveReliabilityAuditError("OBSERVATION_FIELD_INVALID")
    for key in (
        "wsl_running",
        "keepalive_present",
        "user_systemd_reachable",
        "authoritative_service_active",
        "complete",
    ):
        if not isinstance(payload[key], bool):
            raise WslKeepaliveReliabilityAuditError("OBSERVATION_FIELD_INVALID")
    for key in ("sequence", "observed_at_epoch_seconds", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WslKeepaliveReliabilityAuditError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
