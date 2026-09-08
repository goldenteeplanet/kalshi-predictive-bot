from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4iy-component-recovery-workstream-gate-v1"
REQUIRED_COMPONENTS = frozenset(
    {
        "RECOVERY_ACTION_CAPABILITY_MODEL",
        "WSL_WAKE_DRY_RUN_PLANNER",
        "KEEPALIVE_RESTORATION_DRY_RUN_PLANNER",
        "USER_SYSTEMD_RECOVERY_PLANNER",
        "SCHEDULER_RESTORATION_PLANNER",
        "DATABASE_READABILITY_RECOVERY_PLANNER",
        "RECOVERY_TIMEOUT_PROPAGATION",
        "RECOVERY_CANCELLATION_PROPAGATION",
        "COMPONENT_RECOVERY_DIFFERENTIAL_REPLAY",
    }
)
GateStatus = Literal["READY", "NOT_READY", "INCOMPLETE", "TAMPERED"]


class ComponentRecoveryWorkstreamGateError(ValueError):
    """Stable fail-closed component recovery workstream gate error."""


@dataclass(frozen=True)
class ComponentRecoveryEvidence:
    component: str
    artifact_hash: str
    verified: bool
    complete: bool
    safety_proven: bool
    dry_run_only: bool
    evidence_hash: str


@dataclass(frozen=True)
class ComponentRecoveryWorkstreamDecision:
    status: GateStatus
    reasons: tuple[str, ...]
    component_count: int
    component_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    component_recovery_workstream_ready: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    wsl_shutdown_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_component_recovery_evidence(**fields: Any) -> ComponentRecoveryEvidence:
    _validate_fields(fields)
    return ComponentRecoveryEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_component_recovery_workstream_gate(
    evidence: Sequence[Any], *, max_records: int = 9
) -> ComponentRecoveryWorkstreamDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_GATE_BOUND_INVALID")
    if isinstance(evidence, str | bytes) or len(evidence) > max_records:
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_GATE_RECORD_BOUND_EXCEEDED")
    records = [_validated_evidence(item) for item in evidence]
    components = [item.component for item in records]
    duplicates = len(set(components)) != len(components)
    unknown = sorted(set(components) - REQUIRED_COMPONENTS)
    missing = sorted(REQUIRED_COMPONENTS - set(components))
    incomplete = sorted(item.component for item in records if not item.complete)
    unverified = sorted(item.component for item in records if not item.verified)
    unsafe = sorted(
        item.component for item in records if not item.safety_proven or not item.dry_run_only
    )
    if duplicates or unknown:
        status: GateStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("COMPONENT_RECOVERY_COMPONENT_DUPLICATE")
        reasons.extend(f"COMPONENT_RECOVERY_COMPONENT_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"COMPONENT_RECOVERY_COMPONENT_MISSING:{item}" for item in missing)]
        reasons.extend(f"COMPONENT_RECOVERY_COMPONENT_INCOMPLETE:{item}" for item in incomplete)
    elif unverified or unsafe:
        status = "NOT_READY"
        reasons = [*(f"COMPONENT_RECOVERY_COMPONENT_UNVERIFIED:{item}" for item in unverified)]
        reasons.extend(f"COMPONENT_RECOVERY_SAFETY_UNPROVEN:{item}" for item in unsafe)
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
        "component_recovery_workstream_ready": ready,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "wsl_shutdown_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return ComponentRecoveryWorkstreamDecision(
        status=status,
        reasons=tuple(reasons),
        component_count=len(records),
        component_hashes=pairs,
        evidence_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        component_recovery_workstream_ready=ready,
    )


def validate_component_recovery_workstream_decision(value: Any) -> None:
    if not isinstance(value, ComponentRecoveryWorkstreamDecision):
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_GATE_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.wsl_shutdown_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise ComponentRecoveryWorkstreamGateError(
            "COMPONENT_RECOVERY_GATE_SAFETY_BOUNDARY_INVALID"
        )
    if value.component_recovery_workstream_ready != (value.status == "READY"):
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_GATE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["component_hashes"] = [list(item) for item in unsigned["component_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_GATE_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> ComponentRecoveryEvidence:
    if not isinstance(value, ComponentRecoveryEvidence):
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_COMPONENT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_COMPONENT_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "component",
        "artifact_hash",
        "verified",
        "complete",
        "safety_proven",
        "dry_run_only",
    }
    if (
        set(fields) != required
        or not isinstance(fields["component"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["component"]) is None
        or not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_COMPONENT_FIELD_INVALID")
    for key in ("verified", "complete", "safety_proven", "dry_run_only"):
        if not isinstance(fields[key], bool):
            raise ComponentRecoveryWorkstreamGateError("COMPONENT_RECOVERY_COMPONENT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
