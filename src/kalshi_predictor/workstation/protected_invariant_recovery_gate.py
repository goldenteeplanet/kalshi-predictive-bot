from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .writer_exclusivity_recovery_gate import (
    WriterExclusivityGateResult,
    validate_writer_exclusivity_gate_result,
)

GATE_SCHEMA_VERSION = "phase4hh-protected-invariant-recovery-gate-v1"
EXPECTED_TICKER = "KXRAINAUSM-26AUG-1"
GateStatus = Literal["PASSED", "DENIED", "STALE", "INCOMPLETE"]


class ProtectedInvariantRecoveryGateError(ValueError):
    """Stable fail-closed protected-invariant recovery gate error."""


@dataclass(frozen=True)
class ProtectedInvariantObservation:
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    paper_orders_count: int
    position_sizing_max_id: int
    position_sizing_count: int
    advanced_risk_max_id: int
    advanced_risk_count: int
    protected_order_id: int
    protected_order_status: str
    protected_order_ticker: str
    protected_order_quantity: int
    protected_forecast_id: int
    protected_fill_count: int
    phase3m_max_id: int
    phase3n_max_id: int
    complete: bool
    source_identity_hash: str
    observation_hash: str


@dataclass(frozen=True)
class ProtectedInvariantGateResult:
    status: GateStatus
    reasons: tuple[str, ...]
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    source_identity_hash: str
    observation_hash: str
    writer_gate_hash: str
    invariant_snapshot_hash: str
    gate_hash: str
    read_only: bool = True
    protected_invariants_proven: bool = False
    writer_exclusivity_proven: bool = False
    combined_prerequisites_passed: bool = False
    alert_required: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_protected_invariant_observation(
    *,
    observed_at_epoch_seconds: int,
    evidence_age_seconds: int,
    paper_orders_count: int,
    position_sizing_max_id: int,
    position_sizing_count: int,
    advanced_risk_max_id: int,
    advanced_risk_count: int,
    protected_order_id: int,
    protected_order_status: str,
    protected_order_ticker: str,
    protected_order_quantity: int,
    protected_forecast_id: int,
    protected_fill_count: int,
    phase3m_max_id: int,
    phase3n_max_id: int,
    complete: bool,
    source_identity_hash: str,
) -> ProtectedInvariantObservation:
    unsigned = {
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "evidence_age_seconds": evidence_age_seconds,
        "paper_orders_count": paper_orders_count,
        "position_sizing_max_id": position_sizing_max_id,
        "position_sizing_count": position_sizing_count,
        "advanced_risk_max_id": advanced_risk_max_id,
        "advanced_risk_count": advanced_risk_count,
        "protected_order_id": protected_order_id,
        "protected_order_status": protected_order_status,
        "protected_order_ticker": protected_order_ticker,
        "protected_order_quantity": protected_order_quantity,
        "protected_forecast_id": protected_forecast_id,
        "protected_fill_count": protected_fill_count,
        "phase3m_max_id": phase3m_max_id,
        "phase3n_max_id": phase3n_max_id,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
    }
    _validate_observation_fields(unsigned)
    return ProtectedInvariantObservation(**unsigned, observation_hash=_hash(unsigned))


def evaluate_protected_invariant_recovery_gate(
    writer_gate: Any,
    observation: Any,
    *,
    max_evidence_age_seconds: int = 120,
) -> ProtectedInvariantGateResult:
    if (
        isinstance(max_evidence_age_seconds, bool)
        or not isinstance(max_evidence_age_seconds, int)
        or max_evidence_age_seconds < 0
    ):
        raise ProtectedInvariantRecoveryGateError("GATE_BOUND_INVALID")
    if not isinstance(writer_gate, WriterExclusivityGateResult):
        raise ProtectedInvariantRecoveryGateError("WRITER_GATE_TYPE_INVALID")
    try:
        validate_writer_exclusivity_gate_result(writer_gate)
    except ValueError as exc:
        raise ProtectedInvariantRecoveryGateError("WRITER_GATE_INVALID") from exc
    item = _validated_observation(observation)
    observed_age = max(item.evidence_age_seconds, writer_gate.evidence_age_seconds)
    failures = _invariant_failures(item)
    invariants_proven = not failures and item.complete
    writer_proven = writer_gate.prerequisite_gate_passed and writer_gate.writer_exclusivity_proven

    if observed_age > max_evidence_age_seconds:
        status: GateStatus = "STALE"
        reasons = ["PROTECTED_INVARIANT_EVIDENCE_STALE"]
    elif not item.complete:
        status = "INCOMPLETE"
        reasons = ["PROTECTED_INVARIANT_EVIDENCE_INCOMPLETE"]
    else:
        if not writer_proven:
            failures.append("WRITER_EXCLUSIVITY_PREREQUISITE_FAILED")
        status = "DENIED" if failures else "PASSED"
        reasons = sorted(failures)

    combined_passed = status == "PASSED" and invariants_proven and writer_proven
    snapshot = _snapshot_payload(item)
    invariant_snapshot_hash = _hash(snapshot)
    unsigned = {
        "schema_version": GATE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "observed_at_epoch_seconds": item.observed_at_epoch_seconds,
        "evidence_age_seconds": observed_age,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "source_identity_hash": item.source_identity_hash,
        "observation_hash": item.observation_hash,
        "writer_gate_hash": writer_gate.gate_hash,
        "invariant_snapshot_hash": invariant_snapshot_hash,
        "read_only": True,
        "protected_invariants_proven": invariants_proven,
        "writer_exclusivity_proven": writer_proven,
        "combined_prerequisites_passed": combined_passed,
        "alert_required": not combined_passed,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return ProtectedInvariantGateResult(
        status=status,
        reasons=tuple(reasons),
        observed_at_epoch_seconds=item.observed_at_epoch_seconds,
        evidence_age_seconds=observed_age,
        max_evidence_age_seconds=max_evidence_age_seconds,
        source_identity_hash=item.source_identity_hash,
        observation_hash=item.observation_hash,
        writer_gate_hash=writer_gate.gate_hash,
        invariant_snapshot_hash=invariant_snapshot_hash,
        gate_hash=_hash(unsigned),
        protected_invariants_proven=invariants_proven,
        writer_exclusivity_proven=writer_proven,
        combined_prerequisites_passed=combined_passed,
        alert_required=not combined_passed,
    )


def validate_protected_invariant_gate_result(result: Any) -> None:
    if not isinstance(result, ProtectedInvariantGateResult):
        raise ProtectedInvariantRecoveryGateError("GATE_RESULT_TYPE_INVALID")
    if result.read_only is not True or any(
        (
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    ):
        raise ProtectedInvariantRecoveryGateError("GATE_SAFETY_BOUNDARY_INVALID")
    passed = (
        not result.reasons
        and not result.alert_required
        and result.protected_invariants_proven
        and result.writer_exclusivity_proven
        and result.combined_prerequisites_passed
    )
    if (result.status == "PASSED") != passed:
        raise ProtectedInvariantRecoveryGateError("GATE_STATUS_INVALID")
    unsigned = asdict(result)
    unsigned.pop("gate_hash")
    unsigned["schema_version"] = GATE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if result.gate_hash != _hash(unsigned):
        raise ProtectedInvariantRecoveryGateError("GATE_HASH_MISMATCH")


def _invariant_failures(item: ProtectedInvariantObservation) -> list[str]:
    expected = {
        "paper_orders_count": 204,
        "position_sizing_max_id": 239,
        "position_sizing_count": 239,
        "advanced_risk_max_id": 239,
        "advanced_risk_count": 239,
        "protected_order_id": 204,
        "protected_order_status": "filled",
        "protected_order_ticker": EXPECTED_TICKER,
        "protected_order_quantity": 1,
        "protected_forecast_id": 523912,
        "protected_fill_count": 1,
        "phase3m_max_id": 231,
        "phase3n_max_id": 231,
    }
    return [
        f"INVARIANT_MISMATCH:{name}"
        for name, expected_value in expected.items()
        if getattr(item, name) != expected_value
    ]


def _snapshot_payload(item: ProtectedInvariantObservation) -> dict[str, Any]:
    payload = asdict(item)
    payload.pop("observation_hash")
    payload.pop("observed_at_epoch_seconds")
    payload.pop("evidence_age_seconds")
    payload.pop("complete")
    payload.pop("source_identity_hash")
    return payload


def _validated_observation(value: Any) -> ProtectedInvariantObservation:
    if not isinstance(value, ProtectedInvariantObservation):
        raise ProtectedInvariantRecoveryGateError("OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("observation_hash")
    _validate_observation_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise ProtectedInvariantRecoveryGateError("OBSERVATION_HASH_MISMATCH")
    return value


def _validate_observation_fields(payload: dict[str, Any]) -> None:
    if not isinstance(payload["complete"], bool):
        raise ProtectedInvariantRecoveryGateError("OBSERVATION_FIELD_INVALID")
    for key in ("protected_order_status", "protected_order_ticker", "source_identity_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ProtectedInvariantRecoveryGateError("OBSERVATION_FIELD_INVALID")
    string_or_boolean_fields = {
        "complete",
        "protected_order_status",
        "protected_order_ticker",
        "source_identity_hash",
    }
    for key, value in payload.items():
        if key in string_or_boolean_fields:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProtectedInvariantRecoveryGateError("OBSERVATION_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
