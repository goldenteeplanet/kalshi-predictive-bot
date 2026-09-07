from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

GATE_SCHEMA_VERSION = "phase4io-diagnostics-workstream-gate-v1"
REQUIRED_COMPONENTS = frozenset(
    {
        "BOUNDED_DIAGNOSTICS_COLLECTOR",
        "PROCESS_TREE_CAPTURE",
        "WSL_STATUS_CAPTURE",
        "SYSTEMD_STATUS_CAPTURE",
        "SCHEDULER_JOURNAL_CAPTURE",
        "DISK_MEMORY_CAPTURE",
        "NETWORK_DNS_CAPTURE",
        "CLOCK_SOURCE_CAPTURE",
        "DIAGNOSTIC_REDACTION_AUDIT",
    }
)
GateStatus = Literal["READY", "NOT_READY", "INCOMPLETE", "TAMPERED"]


class DiagnosticsWorkstreamGateError(ValueError):
    """Stable fail-closed diagnostics workstream gate error."""


@dataclass(frozen=True)
class DiagnosticsComponentEvidence:
    component: str
    artifact_hash: str
    verified: bool
    complete: bool
    bounds_proven: bool
    redaction_proven: bool
    evidence_hash: str


@dataclass(frozen=True)
class DiagnosticsWorkstreamDecision:
    status: GateStatus
    reasons: tuple[str, ...]
    component_count: int
    component_hashes: tuple[tuple[str, str], ...]
    evidence_set_hash: str
    decision_hash: str
    read_only: bool = True
    diagnostics_workstream_ready: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_diagnostics_component_evidence(**fields: Any) -> DiagnosticsComponentEvidence:
    _validate_fields(fields)
    return DiagnosticsComponentEvidence(**fields, evidence_hash=_hash(fields))


def evaluate_diagnostics_workstream_gate(
    evidence: Sequence[Any], *, max_records: int = 9
) -> DiagnosticsWorkstreamDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_GATE_BOUND_INVALID")
    if isinstance(evidence, str | bytes) or len(evidence) > max_records:
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_GATE_RECORD_BOUND_EXCEEDED")
    records = [_validated_evidence(item) for item in evidence]
    components = [item.component for item in records]
    duplicates = len(set(components)) != len(components)
    unknown = sorted(set(components) - REQUIRED_COMPONENTS)
    missing = sorted(REQUIRED_COMPONENTS - set(components))
    incomplete = sorted(item.component for item in records if not item.complete)
    unverified = sorted(item.component for item in records if not item.verified)
    unsafe = sorted(
        item.component for item in records if not item.bounds_proven or not item.redaction_proven
    )
    if duplicates or unknown:
        status: GateStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("DIAGNOSTICS_COMPONENT_DUPLICATE")
        reasons.extend(f"DIAGNOSTICS_COMPONENT_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"DIAGNOSTICS_COMPONENT_MISSING:{item}" for item in missing)]
        reasons.extend(f"DIAGNOSTICS_COMPONENT_INCOMPLETE:{item}" for item in incomplete)
    elif unverified or unsafe:
        status = "NOT_READY"
        reasons = [*(f"DIAGNOSTICS_COMPONENT_UNVERIFIED:{item}" for item in unverified)]
        reasons.extend(f"DIAGNOSTICS_SAFETY_UNPROVEN:{item}" for item in unsafe)
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
        "diagnostics_workstream_ready": ready,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return DiagnosticsWorkstreamDecision(
        status=status,
        reasons=tuple(reasons),
        component_count=len(records),
        component_hashes=pairs,
        evidence_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        diagnostics_workstream_ready=ready,
    )


def validate_diagnostics_workstream_decision(value: Any) -> None:
    if not isinstance(value, DiagnosticsWorkstreamDecision):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_GATE_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_GATE_SAFETY_BOUNDARY_INVALID")
    if value.diagnostics_workstream_ready != (value.status == "READY"):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_GATE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["component_hashes"] = [list(item) for item in unsigned["component_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_GATE_DECISION_HASH_MISMATCH")


def _validated_evidence(value: Any) -> DiagnosticsComponentEvidence:
    if not isinstance(value, DiagnosticsComponentEvidence):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_COMPONENT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("evidence_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_COMPONENT_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "component",
        "artifact_hash",
        "verified",
        "complete",
        "bounds_proven",
        "redaction_proven",
    }
    if (
        set(fields) != required
        or not isinstance(fields["component"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["component"]) is None
    ):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_COMPONENT_FIELD_INVALID")
    if (
        not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_COMPONENT_FIELD_INVALID")
    for key in ("verified", "complete", "bounds_proven", "redaction_proven"):
        if not isinstance(fields[key], bool):
            raise DiagnosticsWorkstreamGateError("DIAGNOSTICS_COMPONENT_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
