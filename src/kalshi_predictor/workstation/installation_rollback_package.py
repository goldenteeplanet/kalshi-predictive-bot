from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

PACKAGE_SCHEMA_VERSION = "phase4kb-installation-rollback-package-v1"
REQUIRED_OPERATIONS = (
    "DISABLE_STARTUP_TASK",
    "DELETE_STARTUP_TASK",
    "RESTORE_PREVIOUS_SIGNED_CONFIGURATION",
    "PRESERVE_INCIDENT_JOURNAL",
    "PRESERVE_RESTART_HISTORY",
    "VERIFY_STARTUP_TASK_ABSENT",
    "VERIFY_SUPERVISOR_PROCESS_ABSENT",
)
PackageStatus = Literal["READY", "DENIED", "INCOMPLETE", "TAMPERED"]


class InstallationRollbackPackageError(ValueError):
    """Stable fail-closed installation rollback package error."""


@dataclass(frozen=True)
class RollbackPackageRequest:
    installation_manifest_hash: str
    startup_task_proposal_hash: str
    installed_configuration_hash: str
    previous_configuration_hash: str
    incident_journal_hash: str
    restart_history_hash: str
    operations: tuple[str, ...]
    evidence_preservation_required: bool
    execution_requested: bool
    complete: bool
    request_hash: str


@dataclass(frozen=True)
class RollbackPackageDecision:
    status: PackageStatus
    reasons: tuple[str, ...]
    request_hash: str
    operations: tuple[str, ...]
    preserved_evidence_hashes: tuple[str, ...]
    package_manifest_hash: str
    decision_hash: str
    read_only: bool = True
    package_ready: bool = False
    evidence_preserved: bool = True
    rollback_execution_authorized: bool = False
    task_mutation_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_rollback_package_request(**fields: Any) -> RollbackPackageRequest:
    normalized = dict(fields)
    if isinstance(normalized.get("operations"), list):
        normalized["operations"] = tuple(normalized["operations"])
    _validate_fields(normalized)
    unsigned = {**normalized, "operations": list(normalized["operations"])}
    return RollbackPackageRequest(**normalized, request_hash=_hash(unsigned))


def evaluate_installation_rollback_package(
    request: Any,
) -> RollbackPackageDecision:
    item = _validated_request(request)
    duplicates = len(set(item.operations)) != len(item.operations)
    unknown = sorted(set(item.operations) - set(REQUIRED_OPERATIONS))
    if duplicates or unknown:
        status: PackageStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("ROLLBACK_PACKAGE_OPERATION_DUPLICATE")
        reasons.extend(f"ROLLBACK_PACKAGE_OPERATION_UNKNOWN:{operation}" for operation in unknown)
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["ROLLBACK_PACKAGE_REQUEST_INCOMPLETE"]
    else:
        reasons = []
        if item.operations != REQUIRED_OPERATIONS:
            reasons.append("ROLLBACK_PACKAGE_OPERATION_ORDER_INVALID")
        if item.installed_configuration_hash == item.previous_configuration_hash:
            reasons.append("ROLLBACK_PACKAGE_CONFIGURATION_SNAPSHOT_NOT_DISTINCT")
        if not item.evidence_preservation_required:
            reasons.append("ROLLBACK_PACKAGE_EVIDENCE_PRESERVATION_REQUIRED")
        if item.execution_requested:
            reasons.append("ROLLBACK_PACKAGE_EXECUTION_REFUSED")
        status = "DENIED" if reasons else "READY"
    ready = status == "READY"
    preserved = tuple(sorted((item.incident_journal_hash, item.restart_history_hash)))
    manifest = {
        "installation_manifest_hash": item.installation_manifest_hash,
        "startup_task_proposal_hash": item.startup_task_proposal_hash,
        "installed_configuration_hash": item.installed_configuration_hash,
        "previous_configuration_hash": item.previous_configuration_hash,
        "operations": list(REQUIRED_OPERATIONS),
        "preserved_evidence_hashes": list(preserved),
        "execution_disabled": True,
    }
    unsigned = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "request_hash": item.request_hash,
        "operations": list(REQUIRED_OPERATIONS),
        "preserved_evidence_hashes": list(preserved),
        "package_manifest_hash": _hash(manifest),
        "read_only": True,
        "package_ready": ready,
        "evidence_preserved": True,
        "rollback_execution_authorized": False,
        "task_mutation_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RollbackPackageDecision(
        status=status,
        reasons=tuple(reasons),
        request_hash=item.request_hash,
        operations=REQUIRED_OPERATIONS,
        preserved_evidence_hashes=preserved,
        package_manifest_hash=unsigned["package_manifest_hash"],
        decision_hash=_hash(unsigned),
        package_ready=ready,
    )


def validate_rollback_package_decision(value: Any) -> None:
    if not isinstance(value, RollbackPackageDecision):
        raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.operations != REQUIRED_OPERATIONS
        or value.package_ready != (value.status == "READY")
        or value.evidence_preserved is not True
        or any(
            (
                value.rollback_execution_authorized,
                value.task_mutation_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = PACKAGE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["operations"] = list(unsigned["operations"])
    unsigned["preserved_evidence_hashes"] = list(unsigned["preserved_evidence_hashes"])
    if value.decision_hash != _hash(unsigned):
        raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_DECISION_HASH_MISMATCH")


def _validated_request(value: Any) -> RollbackPackageRequest:
    if not isinstance(value, RollbackPackageRequest):
        raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_REQUEST_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("request_hash")
    unsigned["operations"] = tuple(unsigned["operations"])
    _validate_fields(unsigned)
    unsigned["operations"] = list(unsigned["operations"])
    if supplied != _hash(unsigned):
        raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_REQUEST_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "installation_manifest_hash",
        "startup_task_proposal_hash",
        "installed_configuration_hash",
        "previous_configuration_hash",
        "incident_journal_hash",
        "restart_history_hash",
        "operations",
        "evidence_preservation_required",
        "execution_requested",
        "complete",
    }
    if set(fields) != required:
        raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_REQUEST_FIELD_INVALID")
    for key in (
        "installation_manifest_hash",
        "startup_task_proposal_hash",
        "installed_configuration_hash",
        "previous_configuration_hash",
        "incident_journal_hash",
        "restart_history_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_REQUEST_FIELD_INVALID")
    if (
        not isinstance(fields["operations"], tuple)
        or len(fields["operations"]) > 16
        or any(
            not isinstance(operation, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", operation) is None
            for operation in fields["operations"]
        )
    ):
        raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_REQUEST_FIELD_INVALID")
    for key in ("evidence_preservation_required", "execution_requested", "complete"):
        if not isinstance(fields[key], bool):
            raise InstallationRollbackPackageError("ROLLBACK_PACKAGE_REQUEST_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
