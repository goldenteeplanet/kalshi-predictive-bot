from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

ENFORCEMENT_SCHEMA_VERSION = "phase4jw-supervisor-singleton-enforcement-v1"
REQUIRED_INSTANCE_POLICY = "IGNORE_NEW"
MAXIMUM_INSTANCES = 1
EnforcementStatus = Literal["ENFORCED", "NOT_ENFORCED", "INCOMPLETE", "TAMPERED"]


class SupervisorSingletonEnforcementError(ValueError):
    """Stable fail-closed supervisor singleton enforcement error."""


@dataclass(frozen=True)
class SupervisorSingletonEvidence:
    task_proposal_hash: str
    exclusion_lock_hash: str
    owner_identity_hash: str
    instance_policy: str
    maximum_instances: int
    exclusion_lock_required: bool
    exclusive_owner_proven: bool
    evidence_complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class SupervisorSingletonDecision:
    status: EnforcementStatus
    reasons: tuple[str, ...]
    evidence_hash: str
    instance_policy: str
    maximum_instances: int
    enforcement_hash: str
    decision_hash: str
    read_only: bool = True
    singleton_enforced: bool = False
    second_instance_denied: bool = True
    task_activation_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_supervisor_singleton_evidence(**fields: Any) -> SupervisorSingletonEvidence:
    _validate_fields(fields)
    return SupervisorSingletonEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_supervisor_singleton_enforcement(
    evidence: Any,
) -> SupervisorSingletonDecision:
    item = _validated_evidence(evidence)
    if not item.evidence_complete:
        status: EnforcementStatus = "INCOMPLETE"
        reasons = ["SUPERVISOR_SINGLETON_EVIDENCE_INCOMPLETE"]
    else:
        reasons = []
        if item.instance_policy != REQUIRED_INSTANCE_POLICY:
            reasons.append("SUPERVISOR_SINGLETON_INSTANCE_POLICY_INVALID")
        if item.maximum_instances != MAXIMUM_INSTANCES:
            reasons.append("SUPERVISOR_SINGLETON_MAXIMUM_INVALID")
        if not item.exclusion_lock_required:
            reasons.append("SUPERVISOR_SINGLETON_LOCK_NOT_REQUIRED")
        if not item.exclusive_owner_proven:
            reasons.append("SUPERVISOR_SINGLETON_OWNER_UNPROVEN")
        status = "NOT_ENFORCED" if reasons else "ENFORCED"
    enforced = status == "ENFORCED"
    enforcement = {
        "instance_policy": REQUIRED_INSTANCE_POLICY,
        "maximum_instances": MAXIMUM_INSTANCES,
        "exclusion_lock_hash": item.exclusion_lock_hash,
        "owner_identity_hash": item.owner_identity_hash,
    }
    unsigned = {
        "schema_version": ENFORCEMENT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evidence_hash": item.evidence_hash,
        "instance_policy": REQUIRED_INSTANCE_POLICY,
        "maximum_instances": MAXIMUM_INSTANCES,
        "enforcement_hash": _hash(enforcement),
        "read_only": True,
        "singleton_enforced": enforced,
        "second_instance_denied": True,
        "task_activation_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return SupervisorSingletonDecision(
        status=status,
        reasons=tuple(reasons),
        evidence_hash=item.evidence_hash,
        instance_policy=REQUIRED_INSTANCE_POLICY,
        maximum_instances=MAXIMUM_INSTANCES,
        enforcement_hash=unsigned["enforcement_hash"],
        decision_hash=_hash(unsigned),
        singleton_enforced=enforced,
    )


def validate_supervisor_singleton_decision(value: Any) -> None:
    if not isinstance(value, SupervisorSingletonDecision):
        raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.instance_policy != REQUIRED_INSTANCE_POLICY
        or value.maximum_instances != MAXIMUM_INSTANCES
        or value.singleton_enforced != (value.status == "ENFORCED")
        or value.second_instance_denied is not True
        or any(
            (
                value.task_activation_authorized,
                value.restart_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = ENFORCEMENT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> SupervisorSingletonEvidence:
    if not isinstance(value, SupervisorSingletonEvidence):
        raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "task_proposal_hash",
        "exclusion_lock_hash",
        "owner_identity_hash",
        "instance_policy",
        "maximum_instances",
        "exclusion_lock_required",
        "exclusive_owner_proven",
        "evidence_complete",
    }
    if set(fields) != required:
        raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_EVIDENCE_FIELD_INVALID")
    for key in ("task_proposal_hash", "exclusion_lock_hash", "owner_identity_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["instance_policy"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", fields["instance_policy"]) is None
        or isinstance(fields["maximum_instances"], bool)
        or not isinstance(fields["maximum_instances"], int)
        or not 0 <= fields["maximum_instances"] <= 16
    ):
        raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_EVIDENCE_FIELD_INVALID")
    for key in ("exclusion_lock_required", "exclusive_owner_proven", "evidence_complete"):
        if not isinstance(fields[key], bool):
            raise SupervisorSingletonEnforcementError("SUPERVISOR_SINGLETON_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
