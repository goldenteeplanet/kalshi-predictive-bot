from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

LOCK_SCHEMA_VERSION = "phase4jp-concurrent-supervisor-exclusion-lock-v1"
LockStatus = Literal["ACQUIRED", "DRY_RUN_READY", "HELD", "INCOMPLETE", "TAMPERED"]


class SupervisorExclusionLockError(ValueError):
    """Stable fail-closed supervisor exclusion lock error."""


@dataclass(frozen=True)
class SupervisorLockRequest:
    lock_id_hash: str
    owner_identity_hash: str
    host_identity_hash: str
    acquired_at_epoch: int
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class SupervisorLockReceipt:
    status: LockStatus
    reasons: tuple[str, ...]
    request_hash: str
    lock_path_hash: str
    lock_record_hash: str
    existing_lock_hash: str
    receipt_hash: str
    dry_run: bool = True
    exclusive_create: bool = True
    lock_acquired: bool = False
    lock_steal_permitted: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_supervisor_lock_request(**fields: Any) -> SupervisorLockRequest:
    _validate_request_fields(fields)
    return SupervisorLockRequest(**fields, request_hash=_hash(fields))


def acquire_supervisor_exclusion_lock(
    path: Path,
    request: Any,
    *,
    allowed_root: Path,
    dry_run: bool = True,
    max_lock_bytes: int = 4096,
) -> SupervisorLockReceipt:
    if not isinstance(dry_run, bool):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_DRY_RUN_INVALID")
    if (
        isinstance(max_lock_bytes, bool)
        or not isinstance(max_lock_bytes, int)
        or max_lock_bytes <= 0
    ):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_BOUND_INVALID")
    item = _validated_request(request)
    target = _validated_target(path, allowed_root)
    record = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "lock_id_hash": item.lock_id_hash,
        "owner_identity_hash": item.owner_identity_hash,
        "host_identity_hash": item.host_identity_hash,
        "acquired_at_epoch": item.acquired_at_epoch,
        "complete": item.complete,
        "request_hash": item.request_hash,
    }
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > max_lock_bytes:
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_BOUND_EXCEEDED")
    existing_hash = "0" * 64
    if target.exists():
        existing = _read_existing(target, max_lock_bytes)
        existing_hash = _hash_bytes(existing)
        _validate_existing(existing)
        status: LockStatus = "HELD"
        reasons = ["SUPERVISOR_LOCK_ALREADY_HELD"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["SUPERVISOR_LOCK_REQUEST_INCOMPLETE"]
    elif dry_run:
        status = "DRY_RUN_READY"
        reasons = []
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            status = "HELD"
            reasons = ["SUPERVISOR_LOCK_RACE_LOST"]
        else:
            try:
                if os.write(descriptor, encoded) != len(encoded):
                    raise SupervisorExclusionLockError("SUPERVISOR_LOCK_WRITE_INCOMPLETE")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            status = "ACQUIRED"
            reasons = []
    acquired = status == "ACQUIRED"
    unsigned = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "request_hash": item.request_hash,
        "lock_path_hash": _hash(str(target)),
        "lock_record_hash": _hash_bytes(encoded),
        "existing_lock_hash": existing_hash,
        "dry_run": dry_run,
        "exclusive_create": True,
        "lock_acquired": acquired,
        "lock_steal_permitted": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return SupervisorLockReceipt(
        status=status,
        reasons=tuple(reasons),
        request_hash=item.request_hash,
        lock_path_hash=unsigned["lock_path_hash"],
        lock_record_hash=unsigned["lock_record_hash"],
        existing_lock_hash=existing_hash,
        receipt_hash=_hash(unsigned),
        dry_run=dry_run,
        lock_acquired=acquired,
    )


def validate_supervisor_lock_receipt(value: Any) -> None:
    if not isinstance(value, SupervisorLockReceipt):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_RECEIPT_TYPE_INVALID")
    if (
        value.exclusive_create is not True
        or value.lock_steal_permitted is not False
        or value.lock_acquired != (value.status == "ACQUIRED")
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("receipt_hash")
    unsigned["schema_version"] = LOCK_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.receipt_hash != _hash(unsigned):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_RECEIPT_HASH_MISMATCH")


def _validated_request(value: Any) -> SupervisorLockRequest:
    if not isinstance(value, SupervisorLockRequest):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    _validate_request_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_REQUEST_HASH_MISMATCH")
    return value


def _validate_request_fields(fields: dict[str, Any]) -> None:
    required = {
        "lock_id_hash",
        "owner_identity_hash",
        "host_identity_hash",
        "acquired_at_epoch",
        "complete",
    }
    if set(fields) != required:
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_REQUEST_FIELD_INVALID")
    for key in ("lock_id_hash", "owner_identity_hash", "host_identity_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SupervisorExclusionLockError("SUPERVISOR_LOCK_REQUEST_FIELD_INVALID")
    if (
        isinstance(fields["acquired_at_epoch"], bool)
        or not isinstance(fields["acquired_at_epoch"], int)
        or fields["acquired_at_epoch"] < 0
        or not isinstance(fields["complete"], bool)
    ):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_REQUEST_FIELD_INVALID")


def _validated_target(path: Any, root_path: Any) -> Path:
    if (
        not isinstance(path, Path)
        or not isinstance(root_path, Path)
        or not path.is_absolute()
        or not root_path.is_absolute()
    ):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_PATH_INVALID")
    root = root_path.resolve(strict=False)
    target = path.resolve(strict=False)
    if target == root or root not in target.parents or target.suffix != ".lock":
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_PATH_INVALID")
    if target.exists() and target.is_symlink():
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_SYMLINK_REFUSED")
    return target


def _read_existing(path: Path, maximum: int) -> bytes:
    if path.stat().st_size > maximum:
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_BOUND_EXCEEDED")
    return path.read_bytes()


def _validate_existing(data: bytes) -> None:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_EXISTING_CORRUPT") from exc
    required = {
        "schema_version",
        "lock_id_hash",
        "owner_identity_hash",
        "host_identity_hash",
        "acquired_at_epoch",
        "complete",
        "request_hash",
    }
    if (
        set(payload) != required
        or payload["schema_version"] != LOCK_SCHEMA_VERSION
        or payload["complete"] is not True
        or any(
            not isinstance(payload[key], str) or re.fullmatch(r"[0-9a-f]{64}", payload[key]) is None
            for key in ("lock_id_hash", "owner_identity_hash", "host_identity_hash", "request_hash")
        )
    ):
        raise SupervisorExclusionLockError("SUPERVISOR_LOCK_EXISTING_CORRUPT")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
