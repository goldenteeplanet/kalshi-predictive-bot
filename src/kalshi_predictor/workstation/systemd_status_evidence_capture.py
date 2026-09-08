from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CAPTURE_SCHEMA_VERSION = "phase4ii-systemd-status-evidence-capture-v1"
CaptureStatus = Literal["CAPTURED", "PARTIAL", "TAMPERED", "REFUSED"]


class SystemdStatusEvidenceCaptureError(ValueError):
    """Stable fail-closed systemd status evidence capture error."""


@dataclass(frozen=True)
class SystemdUnitEvidence:
    unit_id_hash: str
    scope: Literal["USER", "SYSTEM"]
    load_state: Literal["LOADED", "NOT_FOUND", "ERROR", "UNKNOWN"]
    active_state: Literal["ACTIVE", "INACTIVE", "FAILED", "ACTIVATING", "DEACTIVATING", "UNKNOWN"]
    sub_state_code: str
    observed_at_epoch_seconds: int
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class SystemdStatusCapture:
    status: CaptureStatus
    reasons: tuple[str, ...]
    captured_at_epoch_seconds: int
    unit_count: int
    active_count: int
    failed_count: int
    user_scope_count: int
    system_scope_count: int
    unit_hashes: tuple[str, ...]
    capture_hash: str
    read_only: bool = True
    unit_names_redacted: bool = True
    raw_output_retained: bool = False
    systemd_status_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_systemd_unit_evidence(**fields: Any) -> SystemdUnitEvidence:
    _validate_fields(fields)
    return SystemdUnitEvidence(**fields, evidence_hash=_hash(fields))


def capture_systemd_status_evidence(
    units: Sequence[Any], *, captured_at_epoch_seconds: int, max_units: int = 64
) -> SystemdStatusCapture:
    for value in (captured_at_epoch_seconds, max_units):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SystemdStatusEvidenceCaptureError("SYSTEMD_STATUS_BOUND_INVALID")
    if isinstance(units, str | bytes) or len(units) > max_units:
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_UNIT_BOUND_EXCEEDED")
    records = [_validated_unit(item) for item in units]
    records.sort(key=lambda item: (item.scope, item.unit_id_hash))
    identities = [(item.scope, item.unit_id_hash) for item in records]
    future = any(item.observed_at_epoch_seconds > captured_at_epoch_seconds for item in records)
    if len(set(identities)) != len(identities):
        status: CaptureStatus = "TAMPERED"
        reasons = ["SYSTEMD_UNIT_IDENTITY_DUPLICATE"]
    elif future:
        status = "TAMPERED"
        reasons = ["SYSTEMD_UNIT_EVIDENCE_FROM_FUTURE"]
    elif not records or any(not item.complete for item in records):
        status = "PARTIAL"
        reasons = ["SYSTEMD_UNIT_EVIDENCE_EMPTY_OR_INCOMPLETE"]
    else:
        status = "CAPTURED"
        reasons = []
    complete = status == "CAPTURED"
    hashes = tuple(item.evidence_hash for item in records)
    active = sum(item.active_state == "ACTIVE" for item in records)
    failed = sum(item.active_state == "FAILED" for item in records)
    user = sum(item.scope == "USER" for item in records)
    system = sum(item.scope == "SYSTEM" for item in records)
    unsigned = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "captured_at_epoch_seconds": captured_at_epoch_seconds,
        "unit_count": len(records),
        "active_count": active,
        "failed_count": failed,
        "user_scope_count": user,
        "system_scope_count": system,
        "unit_hashes": list(hashes),
        "read_only": True,
        "unit_names_redacted": True,
        "raw_output_retained": False,
        "systemd_status_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return SystemdStatusCapture(
        status=status,
        reasons=tuple(reasons),
        captured_at_epoch_seconds=captured_at_epoch_seconds,
        unit_count=len(records),
        active_count=active,
        failed_count=failed,
        user_scope_count=user,
        system_scope_count=system,
        unit_hashes=hashes,
        capture_hash=_hash(unsigned),
        systemd_status_complete=complete,
    )


def validate_systemd_status_capture(value: Any) -> None:
    if not isinstance(value, SystemdStatusCapture):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_CAPTURE_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.unit_names_redacted is not True
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
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_SAFETY_BOUNDARY_INVALID")
    if value.systemd_status_complete != (value.status == "CAPTURED"):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("capture_hash")
    unsigned["schema_version"] = CAPTURE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["unit_hashes"] = list(unsigned["unit_hashes"])
    if value.capture_hash != _hash(unsigned):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_CAPTURE_HASH_MISMATCH")


def _validated_unit(value: Any) -> SystemdUnitEvidence:
    if not isinstance(value, SystemdUnitEvidence):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_UNIT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_UNIT_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "unit_id_hash",
        "scope",
        "load_state",
        "active_state",
        "sub_state_code",
        "observed_at_epoch_seconds",
        "complete",
    }
    if (
        set(fields) != required
        or not isinstance(fields["unit_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["unit_id_hash"]) is None
    ):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_UNIT_FIELD_INVALID")
    if (
        fields["scope"] not in {"USER", "SYSTEM"}
        or fields["load_state"] not in {"LOADED", "NOT_FOUND", "ERROR", "UNKNOWN"}
        or fields["active_state"]
        not in {"ACTIVE", "INACTIVE", "FAILED", "ACTIVATING", "DEACTIVATING", "UNKNOWN"}
    ):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_UNIT_FIELD_INVALID")
    if (
        not isinstance(fields["sub_state_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["sub_state_code"]) is None
    ):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_UNIT_FIELD_INVALID")
    timestamp = fields["observed_at_epoch_seconds"]
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
        or not isinstance(fields["complete"], bool)
    ):
        raise SystemdStatusEvidenceCaptureError("SYSTEMD_UNIT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
