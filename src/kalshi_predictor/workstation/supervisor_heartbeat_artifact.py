from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

HEARTBEAT_SCHEMA_VERSION = "phase4jx-supervisor-heartbeat-artifact-v1"
RECEIPT_SCHEMA_VERSION = "phase4jx-supervisor-heartbeat-receipt-v1"


class SupervisorHeartbeatArtifactError(ValueError):
    """Stable fail-closed supervisor heartbeat artifact error."""


@dataclass(frozen=True)
class SupervisorHeartbeat:
    supervisor_instance_hash: str
    boot_identity_hash: str
    exclusion_lock_hash: str
    configuration_hash: str
    sequence: int
    observed_at_epoch: int
    state: str
    complete: bool
    heartbeat_hash: str


@dataclass(frozen=True)
class SupervisorHeartbeatReceipt:
    heartbeat_hash: str
    path_hash: str
    previous_heartbeat_hash: str
    sequence: int
    bytes_written: int
    dry_run: bool
    receipt_hash: str
    schema_version: str = RECEIPT_SCHEMA_VERSION
    atomic_replace: bool = True
    local_only: bool = True
    task_activation_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_supervisor_heartbeat(**fields: Any) -> SupervisorHeartbeat:
    _validate_heartbeat_fields(fields)
    unsigned = {"schema_version": HEARTBEAT_SCHEMA_VERSION, **fields}
    return SupervisorHeartbeat(**fields, heartbeat_hash=_hash(unsigned))


def write_supervisor_heartbeat(
    path: Path,
    heartbeat: Any,
    *,
    allowed_root: Path,
    dry_run: bool = True,
    max_bytes: int = 4096,
) -> SupervisorHeartbeatReceipt:
    if not isinstance(dry_run, bool):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_DRY_RUN_INVALID")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_BOUND_INVALID")
    item = _validated_heartbeat(heartbeat)
    target = _validated_target(path, allowed_root)
    previous = _read_existing(target, max_bytes)
    previous_hash = previous.heartbeat_hash if previous else "0" * 64
    if previous:
        if item.supervisor_instance_hash != previous.supervisor_instance_hash:
            raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_INSTANCE_CHANGED")
        if item.boot_identity_hash != previous.boot_identity_hash:
            raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_BOOT_CHANGED")
        if item.sequence != previous.sequence + 1:
            raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_SEQUENCE_INVALID")
        if item.observed_at_epoch <= previous.observed_at_epoch:
            raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_TIME_NON_MONOTONIC")
    elif item.sequence != 1:
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_SEQUENCE_INVALID")
    if not item.complete:
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_INCOMPLETE")
    encoded = _canonical(item)
    if len(encoded) > max_bytes:
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_BOUND_EXCEEDED")
    written = 0
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{item.heartbeat_hash[:16]}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_TEMP_COLLISION")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_WRITE_INCOMPLETE")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
    unsigned = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "heartbeat_hash": item.heartbeat_hash,
        "path_hash": _hash(str(target)),
        "previous_heartbeat_hash": previous_hash,
        "sequence": item.sequence,
        "bytes_written": written,
        "dry_run": dry_run,
        "atomic_replace": True,
        "local_only": True,
        "task_activation_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return SupervisorHeartbeatReceipt(
        heartbeat_hash=item.heartbeat_hash,
        path_hash=unsigned["path_hash"],
        previous_heartbeat_hash=previous_hash,
        sequence=item.sequence,
        bytes_written=written,
        dry_run=dry_run,
        receipt_hash=_hash(unsigned),
    )


def validate_supervisor_heartbeat_receipt(value: Any) -> None:
    if not isinstance(value, SupervisorHeartbeatReceipt):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_RECEIPT_TYPE_INVALID")
    if (
        value.schema_version != RECEIPT_SCHEMA_VERSION
        or value.atomic_replace is not True
        or value.local_only is not True
        or any(
            (
                value.task_activation_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("receipt_hash")
    if value.receipt_hash != _hash(unsigned):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_RECEIPT_HASH_MISMATCH")


def _validated_heartbeat(value: Any) -> SupervisorHeartbeat:
    if not isinstance(value, SupervisorHeartbeat):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("heartbeat_hash")
    _validate_heartbeat_fields(unsigned)
    if supplied != _hash({"schema_version": HEARTBEAT_SCHEMA_VERSION, **unsigned}):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_HASH_MISMATCH")
    return value


def _validate_heartbeat_fields(fields: dict[str, Any]) -> None:
    required = {
        "supervisor_instance_hash",
        "boot_identity_hash",
        "exclusion_lock_hash",
        "configuration_hash",
        "sequence",
        "observed_at_epoch",
        "state",
        "complete",
    }
    if set(fields) != required:
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_FIELD_INVALID")
    for key in (
        "supervisor_instance_hash",
        "boot_identity_hash",
        "exclusion_lock_hash",
        "configuration_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_FIELD_INVALID")
    for key in ("sequence", "observed_at_epoch"):
        if isinstance(fields[key], bool) or not isinstance(fields[key], int) or fields[key] < 1:
            raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_FIELD_INVALID")
    if fields["state"] not in {"STARTING", "OBSERVING", "ALERT_ONLY", "DEGRADED"} or not isinstance(
        fields["complete"], bool
    ):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_FIELD_INVALID")


def _validated_target(path: Any, root_path: Any) -> Path:
    if (
        not isinstance(path, Path)
        or not isinstance(root_path, Path)
        or not path.is_absolute()
        or not root_path.is_absolute()
    ):
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_PATH_INVALID")
    root = root_path.resolve(strict=False)
    target = path.resolve(strict=False)
    if target == root or root not in target.parents or target.suffix != ".json":
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_PATH_INVALID")
    if target.exists() and target.is_symlink():
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_SYMLINK_REFUSED")
    return target


def _read_existing(path: Path, maximum: int) -> SupervisorHeartbeat | None:
    if not path.exists():
        return None
    if path.stat().st_size > maximum:
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_BOUND_EXCEEDED")
    try:
        value = SupervisorHeartbeat(**json.loads(path.read_bytes()))
        return _validated_heartbeat(value)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorHeartbeatArtifactError("SUPERVISOR_HEARTBEAT_EXISTING_INVALID") from exc


def _canonical(value: SupervisorHeartbeat) -> bytes:
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":")).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
