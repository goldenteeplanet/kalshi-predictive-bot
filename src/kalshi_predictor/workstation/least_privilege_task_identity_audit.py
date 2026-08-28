from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

AUDIT_SCHEMA_VERSION = "phase4ju-least-privilege-task-identity-audit-v1"
REQUIRED_RIGHTS = frozenset(
    {"READ_SUPERVISOR_ARTIFACT", "WRITE_SUPERVISOR_STATE", "EMIT_LOCAL_ALERT"}
)
AuditStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class LeastPrivilegeTaskIdentityAuditError(ValueError):
    """Stable fail-closed least-privilege identity audit error."""


@dataclass(frozen=True)
class TaskIdentityEvidence:
    identity_hash: str
    policy_snapshot_hash: str
    account_type: str
    logon_type: str
    requested_rights: tuple[str, ...]
    administrator: bool
    highest_privileges: bool
    interactive_logon: bool
    network_dependency: bool
    evidence_complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class TaskIdentityAuditDecision:
    status: AuditStatus
    reasons: tuple[str, ...]
    identity_hash: str
    evidence_hash: str
    canonical_rights: tuple[str, ...]
    rights_set_hash: str
    decision_hash: str
    read_only: bool = True
    least_privilege_proven: bool = False
    task_activation_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_task_identity_evidence(**fields: Any) -> TaskIdentityEvidence:
    normalized = dict(fields)
    if isinstance(normalized.get("requested_rights"), list):
        normalized["requested_rights"] = tuple(normalized["requested_rights"])
    _validate_fields(normalized)
    normalized["requested_rights"] = tuple(sorted(normalized["requested_rights"]))
    unsigned = {**normalized, "requested_rights": list(normalized["requested_rights"])}
    return TaskIdentityEvidence(**normalized, evidence_hash=_hash(unsigned))


def evaluate_least_privilege_task_identity(
    evidence: Any,
) -> TaskIdentityAuditDecision:
    item = _validated_evidence(evidence)
    rights = set(item.requested_rights)
    duplicates = len(rights) != len(item.requested_rights)
    unknown = sorted(rights - REQUIRED_RIGHTS)
    missing = sorted(REQUIRED_RIGHTS - rights)
    if duplicates or unknown:
        status: AuditStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("TASK_IDENTITY_RIGHT_DUPLICATE")
        reasons.extend(f"TASK_IDENTITY_RIGHT_UNKNOWN:{right}" for right in unknown)
    elif not item.evidence_complete:
        status = "INCOMPLETE"
        reasons = ["TASK_IDENTITY_EVIDENCE_INCOMPLETE"]
    else:
        reasons = []
        if item.account_type != "STANDARD_USER":
            reasons.append("TASK_IDENTITY_ACCOUNT_TYPE_NOT_STANDARD_USER")
        if item.logon_type != "BATCH":
            reasons.append("TASK_IDENTITY_LOGON_TYPE_NOT_BATCH")
        if item.administrator:
            reasons.append("TASK_IDENTITY_ADMINISTRATOR_REFUSED")
        if item.highest_privileges:
            reasons.append("TASK_IDENTITY_HIGHEST_PRIVILEGES_REFUSED")
        if item.interactive_logon:
            reasons.append("TASK_IDENTITY_INTERACTIVE_LOGON_REFUSED")
        if item.network_dependency:
            reasons.append("TASK_IDENTITY_NETWORK_DEPENDENCY_REFUSED")
        reasons.extend(f"TASK_IDENTITY_RIGHT_MISSING:{right}" for right in missing)
        status = "FAIL" if reasons else "PASS"
    passed = status == "PASS"
    canonical = tuple(sorted(rights))
    unsigned = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "identity_hash": item.identity_hash,
        "evidence_hash": item.evidence_hash,
        "canonical_rights": list(canonical),
        "rights_set_hash": _hash(list(canonical)),
        "read_only": True,
        "least_privilege_proven": passed,
        "task_activation_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return TaskIdentityAuditDecision(
        status=status,
        reasons=tuple(reasons),
        identity_hash=item.identity_hash,
        evidence_hash=item.evidence_hash,
        canonical_rights=canonical,
        rights_set_hash=unsigned["rights_set_hash"],
        decision_hash=_hash(unsigned),
        least_privilege_proven=passed,
    )


def validate_task_identity_audit_decision(value: Any) -> None:
    if not isinstance(value, TaskIdentityAuditDecision):
        raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.least_privilege_proven != (value.status == "PASS")
        or any(
            (
                value.task_activation_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = AUDIT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["canonical_rights"] = list(unsigned["canonical_rights"])
    if value.decision_hash != _hash(unsigned):
        raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> TaskIdentityEvidence:
    if not isinstance(value, TaskIdentityEvidence):
        raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    unsigned["requested_rights"] = tuple(unsigned["requested_rights"])
    _validate_fields(unsigned)
    unsigned["requested_rights"] = list(unsigned["requested_rights"])
    if supplied != _hash(unsigned):
        raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "identity_hash",
        "policy_snapshot_hash",
        "account_type",
        "logon_type",
        "requested_rights",
        "administrator",
        "highest_privileges",
        "interactive_logon",
        "network_dependency",
        "evidence_complete",
    }
    if set(fields) != required:
        raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_EVIDENCE_FIELD_INVALID")
    for key in ("identity_hash", "policy_snapshot_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_EVIDENCE_FIELD_INVALID")
    for key in ("account_type", "logon_type"):
        if (
            not isinstance(fields[key], str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", fields[key]) is None
        ):
            raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["requested_rights"], tuple)
        or len(fields["requested_rights"]) > 16
        or any(
            not isinstance(right, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", right) is None
            for right in fields["requested_rights"]
        )
    ):
        raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_EVIDENCE_FIELD_INVALID")
    for key in (
        "administrator",
        "highest_privileges",
        "interactive_logon",
        "network_dependency",
        "evidence_complete",
    ):
        if not isinstance(fields[key], bool):
            raise LeastPrivilegeTaskIdentityAuditError("TASK_IDENTITY_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
