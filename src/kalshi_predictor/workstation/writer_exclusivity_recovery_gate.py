from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    SchedulerHealthEvidence,
    validate_scheduler_health_evidence,
)

GATE_SCHEMA_VERSION = "phase4hg-writer-exclusivity-recovery-gate-v1"
GateStatus = Literal["PASSED", "DENIED", "STALE", "INCOMPLETE"]


class WriterExclusivityRecoveryGateError(ValueError):
    """Stable fail-closed writer-exclusivity recovery gate error."""


@dataclass(frozen=True)
class WriterInventoryObservation:
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    writer_identities: tuple[str, ...]
    authoritative_writer_identity: str
    complete: bool
    source_identity_hash: str
    observation_hash: str


@dataclass(frozen=True)
class WriterExclusivityGateResult:
    status: GateStatus
    reasons: tuple[str, ...]
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    writer_count: int
    authoritative_writer_identity: str
    writer_identities_hash: str
    source_identity_hash: str
    scheduler_evidence_hash: str
    inventory_observation_hash: str
    gate_hash: str
    read_only: bool = True
    writer_exclusivity_proven: bool = False
    prerequisite_gate_passed: bool = False
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_writer_inventory_observation(
    *,
    observed_at_epoch_seconds: int,
    evidence_age_seconds: int,
    writer_identities: tuple[str, ...],
    authoritative_writer_identity: str,
    complete: bool,
    source_identity_hash: str,
) -> WriterInventoryObservation:
    unsigned = {
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "evidence_age_seconds": evidence_age_seconds,
        "writer_identities": (
            list(writer_identities) if isinstance(writer_identities, tuple) else None
        ),
        "authoritative_writer_identity": authoritative_writer_identity,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
    }
    _validate_inventory_fields(unsigned)
    return WriterInventoryObservation(
        observed_at_epoch_seconds=observed_at_epoch_seconds,
        evidence_age_seconds=evidence_age_seconds,
        writer_identities=writer_identities,
        authoritative_writer_identity=authoritative_writer_identity,
        complete=complete,
        source_identity_hash=source_identity_hash,
        observation_hash=_hash(unsigned),
    )


def evaluate_writer_exclusivity_recovery_gate(
    scheduler_evidence: Any,
    inventory: Any,
    *,
    max_evidence_age_seconds: int = 120,
) -> WriterExclusivityGateResult:
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise WriterExclusivityRecoveryGateError("GATE_BOUND_INVALID")
    if not isinstance(scheduler_evidence, SchedulerHealthEvidence):
        raise WriterExclusivityRecoveryGateError("SCHEDULER_EVIDENCE_TYPE_INVALID")
    try:
        validate_scheduler_health_evidence(scheduler_evidence)
    except ValueError as exc:
        raise WriterExclusivityRecoveryGateError("SCHEDULER_EVIDENCE_INVALID") from exc
    item = _validated_inventory(inventory)
    writer_count = len(item.writer_identities)
    writer_exclusive = (
        writer_count == 1
        and item.writer_identities[0] == AUTHORITATIVE_UNIT
        and item.authoritative_writer_identity == AUTHORITATIVE_UNIT
        and scheduler_evidence.unit_name == AUTHORITATIVE_UNIT
        and scheduler_evidence.writer_exclusive
    )

    observed_age = max(item.evidence_age_seconds, scheduler_evidence.evidence_age_seconds)
    if observed_age > max_evidence_age_seconds:
        status: GateStatus = "STALE"
        reasons = ["WRITER_EXCLUSIVITY_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["WRITER_INVENTORY_INCOMPLETE"]
    else:
        failures = []
        if item.authoritative_writer_identity != AUTHORITATIVE_UNIT:
            failures.append("AUTHORITATIVE_WRITER_IDENTITY_MISMATCH")
        if scheduler_evidence.unit_name != AUTHORITATIVE_UNIT:
            failures.append("SCHEDULER_IDENTITY_MISMATCH")
        if writer_count == 0:
            failures.append("WRITER_MISSING")
        elif writer_count > 1:
            failures.append("MULTIPLE_WRITERS_OBSERVED")
        elif item.writer_identities[0] != AUTHORITATIVE_UNIT:
            failures.append("UNEXPECTED_WRITER_OBSERVED")
        if not scheduler_evidence.writer_exclusive:
            failures.append("SCHEDULER_WRITER_EXCLUSIVITY_UNPROVEN")
        status = "DENIED" if failures else "PASSED"
        reasons = sorted(failures)

    prerequisite_passed = status == "PASSED" and writer_exclusive
    writer_identities_hash = _hash(list(item.writer_identities))
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "observed_at_epoch_seconds": item.observed_at_epoch_seconds,
        "evidence_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "writer_count": writer_count,
        "authoritative_writer_identity": item.authoritative_writer_identity,
        "writer_identities_hash": writer_identities_hash,
        "source_identity_hash": item.source_identity_hash,
        "scheduler_evidence_hash": scheduler_evidence.evidence_hash,
        "inventory_observation_hash": item.observation_hash,
        "read_only": True,
        "writer_exclusivity_proven": writer_exclusive,
        "prerequisite_gate_passed": prerequisite_passed,
        "alert_required": not prerequisite_passed,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return WriterExclusivityGateResult(
        status=status,
        reasons=tuple(reasons),
        observed_at_epoch_seconds=item.observed_at_epoch_seconds,
        evidence_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        writer_count=writer_count,
        authoritative_writer_identity=item.authoritative_writer_identity,
        writer_identities_hash=writer_identities_hash,
        source_identity_hash=item.source_identity_hash,
        scheduler_evidence_hash=scheduler_evidence.evidence_hash,
        inventory_observation_hash=item.observation_hash,
        gate_hash=_hash(unsigned),
        writer_exclusivity_proven=writer_exclusive,
        prerequisite_gate_passed=prerequisite_passed,
        alert_required=not prerequisite_passed,
    )


def validate_writer_exclusivity_gate_result(result: Any) -> None:
    if not isinstance(result, WriterExclusivityGateResult):
        raise WriterExclusivityRecoveryGateError("GATE_RESULT_TYPE_INVALID")
    if result.read_only is not True or any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    ):
        raise WriterExclusivityRecoveryGateError("GATE_SAFETY_BOUNDARY_INVALID")
    passed = (
        not result.reasons
        and not result.alert_required
        and result.writer_exclusivity_proven
        and result.prerequisite_gate_passed
    )
    if (result.status == "PASSED") != passed:
        raise WriterExclusivityRecoveryGateError("GATE_STATUS_INVALID")
    unsigned = asdict(result)
    unsigned.pop("gate_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if result.gate_hash != _hash(unsigned):
        raise WriterExclusivityRecoveryGateError("GATE_HASH_MISMATCH")


def _validated_inventory(value: Any) -> WriterInventoryObservation:
    if not isinstance(value, WriterInventoryObservation):
        raise WriterExclusivityRecoveryGateError("INVENTORY_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    unsigned["writer_identities"] = list(unsigned["writer_identities"])
    _validate_inventory_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise WriterExclusivityRecoveryGateError("INVENTORY_HASH_MISMATCH")
    return value


def _validate_inventory_fields(payload: dict[str, Any]) -> None:
    identities = payload["writer_identities"]
    if not isinstance(identities, list) or any(
        not isinstance(identity, str) or not identity.strip() for identity in identities
    ):
        raise WriterExclusivityRecoveryGateError("INVENTORY_FIELD_INVALID")
    if len(identities) != len(set(identities)):
        raise WriterExclusivityRecoveryGateError("WRITER_IDENTITY_DUPLICATE")
    for key in ("authoritative_writer_identity", "source_identity_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise WriterExclusivityRecoveryGateError("INVENTORY_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise WriterExclusivityRecoveryGateError("INVENTORY_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WriterExclusivityRecoveryGateError("INVENTORY_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
