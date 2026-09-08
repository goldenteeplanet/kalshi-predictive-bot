from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

AUDIT_SCHEMA_VERSION = "phase4in-diagnostic-redaction-audit-v1"
REQUIRED_ARTIFACTS = frozenset(
    {
        "BOUNDED_DIAGNOSTICS",
        "PROCESS_TREE",
        "WSL_STATUS",
        "SYSTEMD_STATUS",
        "SCHEDULER_JOURNAL",
        "DISK_MEMORY",
        "NETWORK_DNS",
        "CLOCK_SOURCE",
    }
)
AuditStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class DiagnosticRedactionAuditError(ValueError):
    """Stable fail-closed diagnostic redaction audit error."""


@dataclass(frozen=True)
class DiagnosticRedactionAttestation:
    artifact_type: str
    artifact_hash: str
    identifiers_redacted: bool
    raw_content_retained: bool
    verified: bool
    complete: bool
    attestation_hash: str


@dataclass(frozen=True)
class DiagnosticRedactionAuditDecision:
    status: AuditStatus
    reasons: tuple[str, ...]
    artifact_count: int
    artifact_hashes: tuple[tuple[str, str], ...]
    attestation_set_hash: str
    decision_hash: str
    read_only: bool = True
    redaction_proven: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_diagnostic_redaction_attestation(**fields: Any) -> DiagnosticRedactionAttestation:
    _validate_fields(fields)
    return DiagnosticRedactionAttestation(**fields, attestation_hash=_hash(fields))


def audit_diagnostic_redaction(
    attestations: Sequence[Any], *, max_records: int = 8
) -> DiagnosticRedactionAuditDecision:
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
        raise DiagnosticRedactionAuditError("REDACTION_AUDIT_BOUND_INVALID")
    if isinstance(attestations, str | bytes) or len(attestations) > max_records:
        raise DiagnosticRedactionAuditError("REDACTION_AUDIT_RECORD_BOUND_EXCEEDED")
    records = [_validated_attestation(item) for item in attestations]
    types = [item.artifact_type for item in records]
    duplicates = len(set(types)) != len(types)
    unknown = sorted(set(types) - REQUIRED_ARTIFACTS)
    missing = sorted(REQUIRED_ARTIFACTS - set(types))
    incomplete = sorted(item.artifact_type for item in records if not item.complete)
    unverified = sorted(item.artifact_type for item in records if not item.verified)
    exposed = sorted(
        item.artifact_type
        for item in records
        if not item.identifiers_redacted or item.raw_content_retained
    )
    if duplicates or unknown:
        status: AuditStatus = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("REDACTION_ARTIFACT_DUPLICATE")
        reasons.extend(f"REDACTION_ARTIFACT_UNKNOWN:{item}" for item in unknown)
    elif missing or incomplete:
        status = "INCOMPLETE"
        reasons = [*(f"REDACTION_ARTIFACT_MISSING:{item}" for item in missing)]
        reasons.extend(f"REDACTION_ARTIFACT_INCOMPLETE:{item}" for item in incomplete)
    elif unverified or exposed:
        status = "FAIL"
        reasons = [*(f"REDACTION_ARTIFACT_UNVERIFIED:{item}" for item in unverified)]
        reasons.extend(f"REDACTION_BOUNDARY_FAILED:{item}" for item in exposed)
    else:
        status = "PASS"
        reasons = []
    ordered = sorted(records, key=lambda item: item.artifact_type)
    pairs = tuple((item.artifact_type, item.artifact_hash) for item in ordered)
    set_hash = _hash([asdict(item) for item in ordered])
    proven = status == "PASS"
    unsigned = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "artifact_count": len(records),
        "artifact_hashes": [list(item) for item in pairs],
        "attestation_set_hash": set_hash,
        "read_only": True,
        "redaction_proven": proven,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return DiagnosticRedactionAuditDecision(
        status=status,
        reasons=tuple(reasons),
        artifact_count=len(records),
        artifact_hashes=pairs,
        attestation_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        redaction_proven=proven,
    )


def validate_diagnostic_redaction_audit_decision(value: Any) -> None:
    if not isinstance(value, DiagnosticRedactionAuditDecision):
        raise DiagnosticRedactionAuditError("REDACTION_AUDIT_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise DiagnosticRedactionAuditError("REDACTION_AUDIT_SAFETY_BOUNDARY_INVALID")
    if value.redaction_proven != (value.status == "PASS"):
        raise DiagnosticRedactionAuditError("REDACTION_AUDIT_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = AUDIT_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["artifact_hashes"] = [list(item) for item in unsigned["artifact_hashes"]]
    if value.decision_hash != _hash(unsigned):
        raise DiagnosticRedactionAuditError("REDACTION_AUDIT_DECISION_HASH_MISMATCH")


def _validated_attestation(value: Any) -> DiagnosticRedactionAttestation:
    if not isinstance(value, DiagnosticRedactionAttestation):
        raise DiagnosticRedactionAuditError("REDACTION_ATTESTATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("attestation_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise DiagnosticRedactionAuditError("REDACTION_ATTESTATION_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "artifact_type",
        "artifact_hash",
        "identifiers_redacted",
        "raw_content_retained",
        "verified",
        "complete",
    }
    if (
        set(fields) != required
        or not isinstance(fields["artifact_type"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["artifact_type"]) is None
    ):
        raise DiagnosticRedactionAuditError("REDACTION_ATTESTATION_FIELD_INVALID")
    if (
        not isinstance(fields["artifact_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["artifact_hash"]) is None
    ):
        raise DiagnosticRedactionAuditError("REDACTION_ATTESTATION_FIELD_INVALID")
    for key in ("identifiers_redacted", "raw_content_retained", "verified", "complete"):
        if not isinstance(fields[key], bool):
            raise DiagnosticRedactionAuditError("REDACTION_ATTESTATION_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
