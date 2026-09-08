from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

CAPTURE_SCHEMA_VERSION = "phase4ik-disk-memory-evidence-capture-v1"
CaptureStatus = Literal["CAPTURED", "PARTIAL", "TAMPERED", "STALE"]


class DiskMemoryEvidenceCaptureError(ValueError):
    """Stable fail-closed disk and memory evidence capture error."""


@dataclass(frozen=True)
class DiskMemorySnapshotEvidence:
    snapshot_id_hash: str
    mount_id_hash: str
    observed_at_epoch_seconds: int
    total_disk_bytes: int
    free_disk_bytes: int
    total_inodes: int
    free_inodes: int
    total_memory_bytes: int
    available_memory_bytes: int
    total_swap_bytes: int
    free_swap_bytes: int
    oom_kill_count: int
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class DiskMemoryCapture:
    status: CaptureStatus
    reasons: tuple[str, ...]
    snapshot_id_hash: str
    mount_id_hash: str
    observed_at_epoch_seconds: int
    evaluated_at_epoch_seconds: int
    free_disk_basis_points: int
    free_inode_basis_points: int
    available_memory_basis_points: int
    free_swap_basis_points: int
    oom_kill_count: int
    evidence_hash: str
    capture_hash: str
    read_only: bool = True
    mount_identity_redacted: bool = True
    raw_output_retained: bool = False
    resource_evidence_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_disk_memory_snapshot_evidence(**fields: Any) -> DiskMemorySnapshotEvidence:
    _validate_fields(fields)
    return DiskMemorySnapshotEvidence(**fields, evidence_hash=_hash(fields))


def capture_disk_memory_evidence(
    evidence: Any, *, evaluated_at_epoch_seconds: int, maximum_age_seconds: int = 120
) -> DiskMemoryCapture:
    for value in (evaluated_at_epoch_seconds, maximum_age_seconds):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DiskMemoryEvidenceCaptureError("RESOURCE_CAPTURE_BOUND_INVALID")
    item = _validated_evidence(evidence)
    contradictory = any(
        free > total
        for free, total in (
            (item.free_disk_bytes, item.total_disk_bytes),
            (item.free_inodes, item.total_inodes),
            (item.available_memory_bytes, item.total_memory_bytes),
            (item.free_swap_bytes, item.total_swap_bytes),
        )
    )
    denominators_missing = any(
        value == 0 for value in (item.total_disk_bytes, item.total_inodes, item.total_memory_bytes)
    )
    disk_bp = _basis_points(item.free_disk_bytes, item.total_disk_bytes)
    inode_bp = _basis_points(item.free_inodes, item.total_inodes)
    memory_bp = _basis_points(item.available_memory_bytes, item.total_memory_bytes)
    swap_bp = _basis_points(item.free_swap_bytes, item.total_swap_bytes)
    if item.observed_at_epoch_seconds > evaluated_at_epoch_seconds or contradictory:
        status: CaptureStatus = "TAMPERED"
        reasons = ["RESOURCE_EVIDENCE_FUTURE_OR_CONTRADICTORY"]
    elif evaluated_at_epoch_seconds - item.observed_at_epoch_seconds > maximum_age_seconds:
        status = "STALE"
        reasons = ["RESOURCE_EVIDENCE_STALE"]
    elif not item.complete or denominators_missing:
        status = "PARTIAL"
        reasons = ["RESOURCE_EVIDENCE_INCOMPLETE_OR_DENOMINATOR_MISSING"]
    else:
        status = "CAPTURED"
        reasons = []
    complete = status == "CAPTURED"
    unsigned = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "snapshot_id_hash": item.snapshot_id_hash,
        "mount_id_hash": item.mount_id_hash,
        "observed_at_epoch_seconds": item.observed_at_epoch_seconds,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "free_disk_basis_points": disk_bp,
        "free_inode_basis_points": inode_bp,
        "available_memory_basis_points": memory_bp,
        "free_swap_basis_points": swap_bp,
        "oom_kill_count": item.oom_kill_count,
        "evidence_hash": item.evidence_hash,
        "read_only": True,
        "mount_identity_redacted": True,
        "raw_output_retained": False,
        "resource_evidence_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return DiskMemoryCapture(
        status=status,
        reasons=tuple(reasons),
        snapshot_id_hash=item.snapshot_id_hash,
        mount_id_hash=item.mount_id_hash,
        observed_at_epoch_seconds=item.observed_at_epoch_seconds,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        free_disk_basis_points=disk_bp,
        free_inode_basis_points=inode_bp,
        available_memory_basis_points=memory_bp,
        free_swap_basis_points=swap_bp,
        oom_kill_count=item.oom_kill_count,
        evidence_hash=item.evidence_hash,
        capture_hash=_hash(unsigned),
        resource_evidence_complete=complete,
    )


def validate_disk_memory_capture(value: Any) -> None:
    if not isinstance(value, DiskMemoryCapture):
        raise DiskMemoryEvidenceCaptureError("RESOURCE_CAPTURE_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.mount_identity_redacted is not True
        or value.raw_output_retained is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise DiskMemoryEvidenceCaptureError("RESOURCE_SAFETY_BOUNDARY_INVALID")
    if value.resource_evidence_complete != (value.status == "CAPTURED"):
        raise DiskMemoryEvidenceCaptureError("RESOURCE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("capture_hash")
    unsigned["schema_version"] = CAPTURE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.capture_hash != _hash(unsigned):
        raise DiskMemoryEvidenceCaptureError("RESOURCE_CAPTURE_HASH_MISMATCH")


def _validated_evidence(value: Any) -> DiskMemorySnapshotEvidence:
    if not isinstance(value, DiskMemorySnapshotEvidence):
        raise DiskMemoryEvidenceCaptureError("RESOURCE_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise DiskMemoryEvidenceCaptureError("RESOURCE_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "snapshot_id_hash",
        "mount_id_hash",
        "observed_at_epoch_seconds",
        "total_disk_bytes",
        "free_disk_bytes",
        "total_inodes",
        "free_inodes",
        "total_memory_bytes",
        "available_memory_bytes",
        "total_swap_bytes",
        "free_swap_bytes",
        "oom_kill_count",
        "complete",
    }
    if set(fields) != required:
        raise DiskMemoryEvidenceCaptureError("RESOURCE_EVIDENCE_FIELD_INVALID")
    for key in ("snapshot_id_hash", "mount_id_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise DiskMemoryEvidenceCaptureError("RESOURCE_EVIDENCE_FIELD_INVALID")
    for key in required - {"snapshot_id_hash", "mount_id_hash", "complete"}:
        item = fields[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise DiskMemoryEvidenceCaptureError("RESOURCE_EVIDENCE_FIELD_INVALID")
    if not isinstance(fields["complete"], bool):
        raise DiskMemoryEvidenceCaptureError("RESOURCE_EVIDENCE_FIELD_INVALID")


def _basis_points(free: int, total: int) -> int:
    return 0 if total == 0 else free * 10_000 // total


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
