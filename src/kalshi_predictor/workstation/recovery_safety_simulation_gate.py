from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4js-recovery-safety-simulation-gate-v1"
REQUIRED_COMPONENTS = frozenset(
    {
        "TEST_HOST_RESTART_PROHIBITION",
        "MOCK_RESTART_EXECUTOR",
        "DISPOSABLE_RECOVERY_SANDBOX",
        "CRASH_BOUNDARY_SIMULATION",
        "POWER_LOSS_STATE_SIMULATION",
        "CORRUPT_COOLDOWN_STATE_REFUSAL",
        "CONCURRENT_SUPERVISOR_EXCLUSION_LOCK",
        "SUPERVISOR_CRASH_RECOVERY",
        "REPLAY_IDEMPOTENCY_VERIFICATION",
    }
)
GateStatus = Literal["READY", "NOT_READY", "INCOMPLETE", "TAMPERED"]


class RecoverySafetySimulationGateError(ValueError):
    """Stable fail-closed recovery safety simulation gate error."""


@dataclass(frozen=True)
class RecoverySafetySimulationEvidence:
    component: str
    artifact_hash: str
    verified: bool
    complete: bool
    safety_proven: bool
    fixture_or_read_only: bool
    evidence_hash: str


@dataclass(frozen=True)
class RecoverySafetySimulationDecision:
    status: GateStatus
    reasons: tuple[str, ...]
    component_count: int
    component_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    recovery_safety_simulation_ready: bool = False
    deployment_review_required: bool = True
    operator_activation_required: bool = True
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_safety_simulation_evidence(
    **fields: Any,
) -> RecoverySafetySimulationEvidence:
    _validate_fields(fields)
    return RecoverySafetySimulationEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_recovery_safety_simulation_gate(
    evidence: Sequence[Any], *, max_records: int = 9
) -> RecoverySafetySimulationDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_GATE_BOUND_INVALID")
    if isinstance(evidence, str | bytes) or len(evidence) > max_records:
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_GATE_RECORD_BOUND_EXCEEDED")
    records = [_validated_evidence(item) for item in evidence]
    names = [item.component for item in records]
    duplicates = len(set(names)) != len(names)
    unknown = sorted(set(names) - REQUIRED_COMPONENTS)
    missing = sorted(REQUIRED_COMPONENTS - set(names))
    incomplete = sorted(item.component for item in records if not item.complete)
    unverified = sorted(item.component for item in records if not item.verified)
    unsafe = sorted(
        item.component
        for item in records
        if not item.safety_proven or not item.fixture_or_read_only
    )
    if duplicates or unknown:
        status: GateStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("RECOVERY_SIMULATION_COMPONENT_DUPLICATE")
        reasons.extend(f"RECOVERY_SIMULATION_COMPONENT_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"RECOVERY_SIMULATION_COMPONENT_MISSING:{item}" for item in missing)]
        reasons.extend(f"RECOVERY_SIMULATION_COMPONENT_INCOMPLETE:{item}" for item in incomplete)
    elif unverified or unsafe:
        status = "NOT_READY"
        reasons = [*(f"RECOVERY_SIMULATION_COMPONENT_UNVERIFIED:{item}" for item in unverified)]
        reasons.extend(f"RECOVERY_SIMULATION_SAFETY_UNPROVEN:{item}" for item in unsafe)
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
        "recovery_safety_simulation_ready": ready,
        "deployment_review_required": True,
        "operator_activation_required": True,
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RecoverySafetySimulationDecision(
        status=status,
        reasons=tuple(reasons),
        component_count=len(records),
        component_hashes=pairs,
        evidence_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        recovery_safety_simulation_ready=ready,
    )


def validate_recovery_safety_simulation_decision(value: Any) -> None:
    if not isinstance(value, RecoverySafetySimulationDecision):
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_DECISION_TYPE_INVALID")
    ready = value.status == "READY"
    if (
        value.read_only is not True
        or value.recovery_safety_simulation_ready != ready
        or value.deployment_review_required is not True
        or value.operator_activation_required is not True
        or any(
            (
                value.restart_authorized,
                value.process_spawn_authorized,
                value.service_control_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["component_hashes"] = [list(item) for item in unsigned["component_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> RecoverySafetySimulationEvidence:
    if not isinstance(value, RecoverySafetySimulationEvidence):
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "component",
        "artifact_hash",
        "verified",
        "complete",
        "safety_proven",
        "fixture_or_read_only",
    }
    if (
        set(fields) != required
        or not isinstance(fields["component"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["component"]) is None
        or not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_EVIDENCE_FIELD_INVALID")
    for key in ("verified", "complete", "safety_proven", "fixture_or_read_only"):
        if not isinstance(fields[key], bool):
            raise RecoverySafetySimulationGateError("RECOVERY_SIMULATION_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
