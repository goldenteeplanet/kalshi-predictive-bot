from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4hu-alerting-workstream-gate-v1"
Component = Literal[
    "SEVERITY_DEDUPLICATION",
    "RETRY_BACKOFF",
    "RATE_LIMIT_STORM_CONTROL",
    "OPERATOR_ACKNOWLEDGEMENT",
    "RECOVERY_CANCELLATION",
    "DELIVERY_AUDIT_EXPORT",
]
GateStatus = Literal["READY", "NOT_READY", "INCOMPLETE", "TAMPERED"]
REQUIRED_COMPONENTS = frozenset(
    {
        "SEVERITY_DEDUPLICATION",
        "RETRY_BACKOFF",
        "RATE_LIMIT_STORM_CONTROL",
        "OPERATOR_ACKNOWLEDGEMENT",
        "RECOVERY_CANCELLATION",
        "DELIVERY_AUDIT_EXPORT",
    }
)


class AlertingWorkstreamGateError(ValueError):
    """Stable fail-closed alerting workstream gate error."""


@dataclass(frozen=True)
class AlertingComponentEvidence:
    component: Component
    artifact_hash: str
    verified: bool
    complete: bool
    evidence_hash: str


@dataclass(frozen=True)
class AlertingWorkstreamDecision:
    status: GateStatus
    reasons: tuple[str, ...]
    evaluated_at_epoch_seconds: int
    component_count: int
    component_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    alerting_workstream_ready: bool = False
    alert_delivery_authorized: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_alerting_component_evidence(
    *, component: Component, artifact_hash: str, verified: bool, complete: bool
) -> AlertingComponentEvidence:
    unsigned = {
        "component": component,
        "artifact_hash": artifact_hash,
        "verified": verified,
        "complete": complete,
    }
    _validate_fields(unsigned)
    return AlertingComponentEvidence(**unsigned, evidence_hash=_hash(unsigned))


def evaluate_alerting_workstream_gate(
    evidence: Sequence[Any], *, evaluated_at_epoch_seconds: int, max_records: int = 6
) -> AlertingWorkstreamDecision:
    for value in (evaluated_at_epoch_seconds, max_records):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AlertingWorkstreamGateError("GATE_BOUND_INVALID")
    if isinstance(evidence, str | bytes) or len(evidence) > max_records:
        raise AlertingWorkstreamGateError("GATE_EVIDENCE_BOUND_EXCEEDED")
    records = [_validated_evidence(item) for item in evidence]
    components = [item.component for item in records]
    if len(set(components)) != len(components):
        status: GateStatus = "TAMPERED"
        reasons = ["ALERTING_COMPONENT_DUPLICATE"]
    else:
        missing = sorted(REQUIRED_COMPONENTS - set(components))
        incomplete = sorted(item.component for item in records if not item.complete)
        unverified = sorted(item.component for item in records if not item.verified)
        if missing or incomplete:
            status = "INCOMPLETE"
            reasons = [*(f"ALERTING_COMPONENT_MISSING:{item}" for item in missing)]
            reasons.extend(f"ALERTING_COMPONENT_INCOMPLETE:{item}" for item in incomplete)
        elif unverified:
            status = "NOT_READY"
            reasons = [f"ALERTING_COMPONENT_UNVERIFIED:{item}" for item in unverified]
        else:
            status = "READY"
            reasons = []
    ordered = sorted(records, key=lambda item: item.component)
    pairs = tuple((item.component, item.artifact_hash) for item in ordered)
    evidence_set_hash = _hash([asdict(item) for item in ordered])
    ready = status == "READY"
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "component_count": len(records),
        "component_hashes": [list(item) for item in pairs],
        "evidence_set_hash": evidence_set_hash,
        "read_only": True,
        "alerting_workstream_ready": ready,
        "alert_delivery_authorized": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return AlertingWorkstreamDecision(
        status=status,
        reasons=tuple(reasons),
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        component_count=len(records),
        component_hashes=pairs,
        evidence_set_hash=evidence_set_hash,
        decision_hash=_hash(unsigned),
        alerting_workstream_ready=ready,
    )


def validate_alerting_workstream_decision(value: Any) -> None:
    if not isinstance(value, AlertingWorkstreamDecision):
        raise AlertingWorkstreamGateError("GATE_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.alert_delivery_authorized,
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise AlertingWorkstreamGateError("GATE_SAFETY_BOUNDARY_INVALID")
    if value.alerting_workstream_ready != (value.status == "READY"):
        raise AlertingWorkstreamGateError("GATE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["component_hashes"] = [list(item) for item in unsigned["component_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise AlertingWorkstreamGateError("GATE_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> AlertingComponentEvidence:
    if not isinstance(value, AlertingComponentEvidence):
        raise AlertingWorkstreamGateError("GATE_EVIDENCE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise AlertingWorkstreamGateError("GATE_EVIDENCE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    if fields["component"] not in REQUIRED_COMPONENTS:
        raise AlertingWorkstreamGateError("GATE_EVIDENCE_FIELD_INVALID")
    if (
        not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise AlertingWorkstreamGateError("GATE_EVIDENCE_FIELD_INVALID")
    if not isinstance(fields["verified"], bool) or not isinstance(fields["complete"], bool):
        raise AlertingWorkstreamGateError("GATE_EVIDENCE_FIELD_INVALID")


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
