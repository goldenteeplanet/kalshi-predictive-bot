from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

CLASSIFIER_SCHEMA_VERSION = "phase4ib-disk-exhaustion-classifier-v1"
DiskStatus = Literal["HEALTHY", "EXHAUSTED", "READ_ONLY", "UNKNOWN", "INCOMPLETE", "TAMPERED"]


class DiskExhaustionClassifierError(ValueError):
    """Stable fail-closed disk exhaustion classifier error."""


@dataclass(frozen=True)
class DiskCapacityEvidence:
    probe_id_hash: str
    mount_code: str
    observed_at_epoch_seconds: int
    total_bytes: int
    free_bytes: int
    total_inodes: int
    free_inodes: int
    read_only: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class DiskExhaustionDecision:
    status: DiskStatus
    reasons: tuple[str, ...]
    mount_code: str
    evidence_hash: str
    evaluated_at_epoch_seconds: int
    minimum_free_bytes: int
    minimum_free_percent: int
    minimum_free_inode_percent: int
    free_percent_basis_points: int
    free_inode_percent_basis_points: int
    decision_hash: str
    read_only: bool = True
    disk_headroom_proven: bool = False
    operator_alert_required: bool = True
    restart_eligible: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_disk_capacity_evidence(**fields: Any) -> DiskCapacityEvidence:
    _validate_fields(fields)
    return DiskCapacityEvidence(**fields, evidence_hash=_hash(fields))


def classify_disk_exhaustion(
    evidence: Any,
    *,
    evaluated_at_epoch_seconds: int,
    minimum_free_bytes: int = 1_073_741_824,
    minimum_free_percent: int = 5,
    minimum_free_inode_percent: int = 5,
    maximum_age_seconds: int = 120,
) -> DiskExhaustionDecision:
    for value in (
        evaluated_at_epoch_seconds,
        minimum_free_bytes,
        minimum_free_percent,
        minimum_free_inode_percent,
        maximum_age_seconds,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DiskExhaustionClassifierError("DISK_CLASSIFIER_BOUND_INVALID")
    if minimum_free_percent > 100 or minimum_free_inode_percent > 100:
        raise DiskExhaustionClassifierError("DISK_CLASSIFIER_BOUND_INVALID")
    item = _validated_evidence(evidence)
    bytes_bp = 0 if item.total_bytes == 0 else item.free_bytes * 10_000 // item.total_bytes
    inode_bp = 0 if item.total_inodes == 0 else item.free_inodes * 10_000 // item.total_inodes
    contradictory = item.free_bytes > item.total_bytes or item.free_inodes > item.total_inodes
    if item.observed_at_epoch_seconds > evaluated_at_epoch_seconds or contradictory:
        status: DiskStatus = "TAMPERED"
        reasons = ["DISK_EVIDENCE_FUTURE_OR_CONTRADICTORY"]
    elif evaluated_at_epoch_seconds - item.observed_at_epoch_seconds > maximum_age_seconds:
        status = "UNKNOWN"
        reasons = ["DISK_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["DISK_EVIDENCE_INCOMPLETE"]
    elif item.total_bytes == 0 or item.total_inodes == 0:
        status = "UNKNOWN"
        reasons = ["DISK_CAPACITY_DENOMINATOR_UNKNOWN"]
    elif item.read_only:
        status = "READ_ONLY"
        reasons = ["DISK_MOUNT_READ_ONLY"]
    elif (
        item.free_bytes < minimum_free_bytes
        or bytes_bp < minimum_free_percent * 100
        or inode_bp < minimum_free_inode_percent * 100
    ):
        status = "EXHAUSTED"
        reasons = ["DISK_HEADROOM_BELOW_THRESHOLD"]
    else:
        status = "HEALTHY"
        reasons = []
    healthy = status == "HEALTHY"
    unsigned = {
        "schema_version": CLASSIFIER_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "mount_code": item.mount_code,
        "evidence_hash": item.evidence_hash,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "minimum_free_bytes": minimum_free_bytes,
        "minimum_free_percent": minimum_free_percent,
        "minimum_free_inode_percent": minimum_free_inode_percent,
        "free_percent_basis_points": bytes_bp,
        "free_inode_percent_basis_points": inode_bp,
        "read_only": True,
        "disk_headroom_proven": healthy,
        "operator_alert_required": not healthy,
        "restart_eligible": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return DiskExhaustionDecision(
        status=status,
        reasons=tuple(reasons),
        mount_code=item.mount_code,
        evidence_hash=item.evidence_hash,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        minimum_free_bytes=minimum_free_bytes,
        minimum_free_percent=minimum_free_percent,
        minimum_free_inode_percent=minimum_free_inode_percent,
        free_percent_basis_points=bytes_bp,
        free_inode_percent_basis_points=inode_bp,
        decision_hash=_hash(unsigned),
        disk_headroom_proven=healthy,
        operator_alert_required=not healthy,
    )


def validate_disk_exhaustion_decision(value: Any) -> None:
    if not isinstance(value, DiskExhaustionDecision):
        raise DiskExhaustionClassifierError("DISK_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.restart_eligible is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise DiskExhaustionClassifierError("DISK_SAFETY_BOUNDARY_INVALID")
    if value.disk_headroom_proven != (value.status == "HEALTHY"):
        raise DiskExhaustionClassifierError("DISK_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = CLASSIFIER_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise DiskExhaustionClassifierError("DISK_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> DiskCapacityEvidence:
    if not isinstance(value, DiskCapacityEvidence):
        raise DiskExhaustionClassifierError("DISK_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise DiskExhaustionClassifierError("DISK_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "probe_id_hash",
        "mount_code",
        "observed_at_epoch_seconds",
        "total_bytes",
        "free_bytes",
        "total_inodes",
        "free_inodes",
        "read_only",
        "complete",
    }
    if (
        set(fields) != required
        or not isinstance(fields["probe_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["probe_id_hash"]) is None
    ):
        raise DiskExhaustionClassifierError("DISK_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["mount_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", fields["mount_code"]) is None
    ):
        raise DiskExhaustionClassifierError("DISK_EVIDENCE_FIELD_INVALID")
    for key in (
        "observed_at_epoch_seconds",
        "total_bytes",
        "free_bytes",
        "total_inodes",
        "free_inodes",
    ):
        value = fields[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DiskExhaustionClassifierError("DISK_EVIDENCE_FIELD_INVALID")
    if not isinstance(fields["read_only"], bool) or not isinstance(fields["complete"], bool):
        raise DiskExhaustionClassifierError("DISK_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
