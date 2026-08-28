from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4ji-restart-authorization-workstream-gate-v1"
REQUIRED_COMPONENTS = frozenset(
    {
        "HOST_RESTART_ELIGIBILITY_MODEL",
        "RESTART_DENIAL_REASON_TAXONOMY",
        "FIVE_MINUTE_RESTART_WARNING",
        "RESTART_CANCELLATION_TOKEN",
        "PERSISTENT_RESTART_INTENT_RECORD",
        "SIX_HOUR_RESTART_COOLDOWN",
        "SEVEN_DAY_RESTART_BUDGET",
        "RESTART_LOOP_CIRCUIT_BREAKER",
        "NON_FORCED_RESTART_COMMAND_ADAPTER",
    }
)
GateStatus = Literal["READY", "NOT_READY", "INCOMPLETE", "TAMPERED"]


class RestartAuthorizationWorkstreamGateError(ValueError):
    """Stable fail-closed restart authorization workstream gate error."""


@dataclass(frozen=True)
class RestartAuthorizationComponentEvidence:
    component: str
    artifact_hash: str
    verified: bool
    complete: bool
    safety_proven: bool
    dry_run_or_read_only: bool
    evidence_hash: str


@dataclass(frozen=True)
class RestartAuthorizationWorkstreamDecision:
    status: GateStatus
    reasons: tuple[str, ...]
    component_count: int
    component_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    restart_authorization_workstream_ready: bool = False
    downstream_simulation_required: bool = True
    operator_activation_required: bool = True
    restart_authorized: bool = False
    process_spawn_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_authorization_component_evidence(
    **fields: Any,
) -> RestartAuthorizationComponentEvidence:
    _validate_fields(fields)
    return RestartAuthorizationComponentEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_restart_authorization_workstream_gate(
    evidence: Sequence[Any], *, max_records: int = 9
) -> RestartAuthorizationWorkstreamDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise RestartAuthorizationWorkstreamGateError("RESTART_AUTHORIZATION_GATE_BOUND_INVALID")
    if isinstance(evidence, (str, bytes)) or len(evidence) > max_records:
        raise RestartAuthorizationWorkstreamGateError(
            "RESTART_AUTHORIZATION_GATE_RECORD_BOUND_EXCEEDED"
        )
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
        if not item.safety_proven or not item.dry_run_or_read_only
    )
    if duplicates or unknown:
        status: GateStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("RESTART_AUTHORIZATION_COMPONENT_DUPLICATE")
        reasons.extend(f"RESTART_AUTHORIZATION_COMPONENT_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"RESTART_AUTHORIZATION_COMPONENT_MISSING:{item}" for item in missing)]
        reasons.extend(f"RESTART_AUTHORIZATION_COMPONENT_INCOMPLETE:{item}" for item in incomplete)
    elif unverified or unsafe:
        status = "NOT_READY"
        reasons = [*(f"RESTART_AUTHORIZATION_COMPONENT_UNVERIFIED:{item}" for item in unverified)]
        reasons.extend(f"RESTART_AUTHORIZATION_SAFETY_UNPROVEN:{item}" for item in unsafe)
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
        "restart_authorization_workstream_ready": ready,
        "downstream_simulation_required": True,
        "operator_activation_required": True,
        "restart_authorized": False,
        "process_spawn_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartAuthorizationWorkstreamDecision(
        status=status,
        reasons=tuple(reasons),
        component_count=len(records),
        component_hashes=pairs,
        evidence_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        restart_authorization_workstream_ready=ready,
    )


def validate_restart_authorization_workstream_decision(value: Any) -> None:
    if not isinstance(value, RestartAuthorizationWorkstreamDecision):
        raise RestartAuthorizationWorkstreamGateError(
            "RESTART_AUTHORIZATION_GATE_DECISION_TYPE_INVALID"
        )
    ready = value.status == "READY"
    if (
        value.read_only is not True
        or value.restart_authorization_workstream_ready != ready
        or value.downstream_simulation_required is not True
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
        raise RestartAuthorizationWorkstreamGateError(
            "RESTART_AUTHORIZATION_GATE_SAFETY_BOUNDARY_INVALID"
        )
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["component_hashes"] = [list(item) for item in unsigned["component_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise RestartAuthorizationWorkstreamGateError(
            "RESTART_AUTHORIZATION_GATE_DECISION_HASH_MISMATCH"
        )


def _validated_evidence(value: Any) -> RestartAuthorizationComponentEvidence:
    if not isinstance(value, RestartAuthorizationComponentEvidence):
        raise RestartAuthorizationWorkstreamGateError(
            "RESTART_AUTHORIZATION_COMPONENT_TYPE_INVALID"
        )
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RestartAuthorizationWorkstreamGateError(
            "RESTART_AUTHORIZATION_COMPONENT_HASH_MISMATCH"
        )
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "component",
        "artifact_hash",
        "verified",
        "complete",
        "safety_proven",
        "dry_run_or_read_only",
    }
    if (
        set(fields) != required
        or not isinstance(fields["component"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["component"]) is None
        or not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise RestartAuthorizationWorkstreamGateError(
            "RESTART_AUTHORIZATION_COMPONENT_FIELD_INVALID"
        )
    for key in ("verified", "complete", "safety_proven", "dry_run_or_read_only"):
        if not isinstance(fields[key], bool):
            raise RestartAuthorizationWorkstreamGateError(
                "RESTART_AUTHORIZATION_COMPONENT_FIELD_INVALID"
            )


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
