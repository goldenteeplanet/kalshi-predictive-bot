from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

SANDBOX_SCHEMA_VERSION = "phase4jl-disposable-recovery-sandbox-v1"
MANIFEST_NAME = "sandbox-manifest.json"
SandboxStatus = Literal["READY", "DRY_RUN_READY", "DENIED", "INCOMPLETE", "TAMPERED"]


class DisposableRecoverySandboxError(ValueError):
    """Stable fail-closed disposable recovery sandbox error."""


@dataclass(frozen=True)
class RecoverySandboxRequest:
    sandbox_id: str
    production_identity_hash: str
    fixture_identity_hash: str
    source_evidence_hash: str
    test_host: bool
    disposable: bool
    dry_run: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class RecoverySandboxReceipt:
    status: SandboxStatus
    reasons: tuple[str, ...]
    sandbox_id_hash: str
    request_hash: str
    sandbox_path_hash: str
    manifest_hash: str
    manifest_written: bool
    receipt_hash: str
    read_only_fixture: bool = True
    disposable: bool = True
    production_isolated: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    database_write_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_sandbox_request(**fields: Any) -> RecoverySandboxRequest:
    _validate_request_fields(fields)
    return RecoverySandboxRequest(**fields, request_hash=_hash(fields))


def provision_disposable_recovery_sandbox(
    root: Path,
    request: Any,
    *,
    allowed_root: Path,
) -> RecoverySandboxReceipt:
    item = _validated_request(request)
    target = _validated_target(root, allowed_root, item.sandbox_id)
    identity_distinct = item.production_identity_hash != item.fixture_identity_hash
    if not item.complete:
        status: SandboxStatus = "INCOMPLETE"
        reasons = ["RECOVERY_SANDBOX_REQUEST_INCOMPLETE"]
    elif not identity_distinct:
        status = "TAMPERED"
        reasons = ["RECOVERY_SANDBOX_PRODUCTION_IDENTITY_COLLISION"]
    elif not item.test_host or not item.disposable:
        status = "DENIED"
        reasons = ["RECOVERY_SANDBOX_TEST_DISPOSABLE_REQUIRED"]
    elif target.exists():
        status = "DENIED"
        reasons = ["RECOVERY_SANDBOX_TARGET_ALREADY_EXISTS"]
    else:
        status = "DRY_RUN_READY" if item.dry_run else "READY"
        reasons = []
    manifest = {
        "schema_version": SANDBOX_SCHEMA_VERSION,
        "sandbox_id": item.sandbox_id,
        "production_identity_hash": item.production_identity_hash,
        "fixture_identity_hash": item.fixture_identity_hash,
        "source_evidence_hash": item.source_evidence_hash,
        "test_host": item.test_host,
        "disposable": item.disposable,
        "read_only_fixture": True,
        "production_isolated": identity_distinct,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    written = status == "READY"
    if written:
        target.mkdir(parents=True, exist_ok=False)
        manifest_path = target / MANIFEST_NAME
        manifest_path.write_bytes(manifest_bytes)
        if manifest_path.is_symlink():
            raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_SYMLINK_REFUSED")
    unsigned = {
        "schema_version": SANDBOX_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "sandbox_id_hash": _hash(item.sandbox_id),
        "request_hash": item.request_hash,
        "sandbox_path_hash": _hash(str(target)),
        "manifest_hash": _hash_bytes(manifest_bytes),
        "manifest_written": written,
        "read_only_fixture": True,
        "disposable": True,
        "production_isolated": identity_distinct,
        "restart_authorized": False,
        "service_control_authorized": False,
        "database_write_authorized": False,
        "execution_authorized": False,
    }
    return RecoverySandboxReceipt(
        status=status,
        reasons=tuple(reasons),
        sandbox_id_hash=unsigned["sandbox_id_hash"],
        request_hash=item.request_hash,
        sandbox_path_hash=unsigned["sandbox_path_hash"],
        manifest_hash=unsigned["manifest_hash"],
        manifest_written=written,
        receipt_hash=_hash(unsigned),
        production_isolated=identity_distinct,
    )


def validate_recovery_sandbox_receipt(value: Any) -> None:
    if not isinstance(value, RecoverySandboxReceipt):
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_RECEIPT_TYPE_INVALID")
    if (
        value.read_only_fixture is not True
        or value.disposable is not True
        or value.manifest_written != (value.status == "READY")
        or any(
            (
                value.restart_authorized,
                value.service_control_authorized,
                value.database_write_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("receipt_hash")
    unsigned["schema_version"] = SANDBOX_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.receipt_hash != _hash(unsigned):
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_RECEIPT_HASH_MISMATCH")


def _validated_request(value: Any) -> RecoverySandboxRequest:
    if not isinstance(value, RecoverySandboxRequest):
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    _validate_request_fields(unsigned)
    if supplied != _hash(unsigned):
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_REQUEST_HASH_MISMATCH")
    return value


def _validate_request_fields(fields: dict[str, Any]) -> None:
    required = {
        "sandbox_id",
        "production_identity_hash",
        "fixture_identity_hash",
        "source_evidence_hash",
        "test_host",
        "disposable",
        "dry_run",
        "complete",
    }
    if set(fields) != required:
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_FIELD_INVALID")
    if (
        not isinstance(fields["sandbox_id"], str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", fields["sandbox_id"]) is None
    ):
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_FIELD_INVALID")
    for key in ("production_identity_hash", "fixture_identity_hash", "source_evidence_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_FIELD_INVALID")
    for key in ("test_host", "disposable", "dry_run", "complete"):
        if not isinstance(fields[key], bool):
            raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_FIELD_INVALID")


def _validated_target(root: Any, allowed_root: Any, sandbox_id: str) -> Path:
    if (
        not isinstance(root, Path)
        or not isinstance(allowed_root, Path)
        or not root.is_absolute()
        or not allowed_root.is_absolute()
    ):
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_PATH_INVALID")
    boundary = allowed_root.resolve(strict=False)
    base = root.resolve(strict=False)
    if base != boundary and boundary not in base.parents:
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_PATH_OUTSIDE_ALLOWED_ROOT")
    if base.exists() and base.is_symlink():
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_SYMLINK_REFUSED")
    target = (base / sandbox_id).resolve(strict=False)
    if boundary not in target.parents:
        raise DisposableRecoverySandboxError("RECOVERY_SANDBOX_PATH_OUTSIDE_ALLOWED_ROOT")
    return target


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
