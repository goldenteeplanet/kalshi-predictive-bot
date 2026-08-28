from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4kc-supervisor-deployment-gate-v1"
REQUIRED_COMPONENTS = frozenset(
    {
        "WINDOWS_STARTUP_TASK_PROPOSAL",
        "LEAST_PRIVILEGE_TASK_IDENTITY_AUDIT",
        "STARTUP_ORDERING_DELAY_MODEL",
        "SUPERVISOR_SINGLETON_ENFORCEMENT",
        "SUPERVISOR_HEARTBEAT_ARTIFACT",
        "SUPERVISOR_SELF_HEALTH_MONITOR",
        "CONFIGURATION_SIGNATURE_VALIDATION",
        "SAFE_CONFIGURATION_RELOAD",
        "INSTALLATION_ROLLBACK_PACKAGE",
    }
)
GateStatus = Literal["READY", "NOT_READY", "INCOMPLETE", "TAMPERED"]


class SupervisorDeploymentGateError(ValueError):
    """Stable fail-closed supervisor deployment gate error."""


@dataclass(frozen=True)
class SupervisorDeploymentEvidence:
    component: str
    artifact_hash: str
    verified: bool
    complete: bool
    safety_proven: bool
    activation_disabled: bool
    evidence_hash: str


@dataclass(frozen=True)
class SupervisorDeploymentDecision:
    status: GateStatus
    reasons: tuple[str, ...]
    component_count: int
    component_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    deployment_evidence_ready: bool = False
    explicit_operator_activation_required: bool = True
    task_activation_authorized: bool = False
    configuration_use_authorized: bool = False
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_supervisor_deployment_evidence(**fields: Any) -> SupervisorDeploymentEvidence:
    _validate_fields(fields)
    return SupervisorDeploymentEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_supervisor_deployment_gate(
    evidence: Sequence[Any], *, max_records: int = 9
) -> SupervisorDeploymentDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_GATE_BOUND_INVALID")
    if isinstance(evidence, (str, bytes)) or len(evidence) > max_records:
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_GATE_RECORD_BOUND_EXCEEDED")
    records = [_validated_evidence(item) for item in evidence]
    names = [item.component for item in records]
    duplicates = len(set(names)) != len(names)
    unknown = sorted(set(names) - REQUIRED_COMPONENTS)
    missing = sorted(REQUIRED_COMPONENTS - set(names))
    incomplete = sorted(item.component for item in records if not item.complete)
    unverified = sorted(item.component for item in records if not item.verified)
    unsafe = sorted(
        item.component for item in records if not item.safety_proven or not item.activation_disabled
    )
    if duplicates or unknown:
        status: GateStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("SUPERVISOR_DEPLOYMENT_COMPONENT_DUPLICATE")
        reasons.extend(f"SUPERVISOR_DEPLOYMENT_COMPONENT_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"SUPERVISOR_DEPLOYMENT_COMPONENT_MISSING:{item}" for item in missing)]
        reasons.extend(f"SUPERVISOR_DEPLOYMENT_COMPONENT_INCOMPLETE:{item}" for item in incomplete)
    elif unverified or unsafe:
        status = "NOT_READY"
        reasons = [*(f"SUPERVISOR_DEPLOYMENT_COMPONENT_UNVERIFIED:{item}" for item in unverified)]
        reasons.extend(f"SUPERVISOR_DEPLOYMENT_SAFETY_UNPROVEN:{item}" for item in unsafe)
    else:
        status = "READY"
        reasons = []
    ordered = sorted(records, key=lambda item: item.component)
    pairs = tuple((item.component, item.artifact_hash) for item in ordered)
    set_hash = _hash([asdict(item) for item in ordered])
    ready = status == "READY"
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "component_count": len(records),
        "component_hashes": [list(item) for item in pairs],
        "evidence_set_hash": set_hash,
        "read_only": True,
        "deployment_evidence_ready": ready,
        "explicit_operator_activation_required": True,
        "task_activation_authorized": False,
        "configuration_use_authorized": False,
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return SupervisorDeploymentDecision(
        status=status,
        reasons=tuple(reasons),
        component_count=len(records),
        component_hashes=pairs,
        evidence_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        deployment_evidence_ready=ready,
    )


def validate_supervisor_deployment_decision(value: Any) -> None:
    if not isinstance(value, SupervisorDeploymentDecision):
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.deployment_evidence_ready != (value.status == "READY")
        or value.explicit_operator_activation_required is not True
        or any(
            (
                value.task_activation_authorized,
                value.configuration_use_authorized,
                value.restart_authorized,
                value.process_spawn_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["component_hashes"] = [list(item) for item in unsigned["component_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> SupervisorDeploymentEvidence:
    if not isinstance(value, SupervisorDeploymentEvidence):
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "component",
        "artifact_hash",
        "verified",
        "complete",
        "safety_proven",
        "activation_disabled",
    }
    if (
        set(fields) != required
        or not isinstance(fields["component"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["component"]) is None
        or not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_EVIDENCE_FIELD_INVALID")
    for key in ("verified", "complete", "safety_proven", "activation_disabled"):
        if not isinstance(fields[key], bool):
            raise SupervisorDeploymentGateError("SUPERVISOR_DEPLOYMENT_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
