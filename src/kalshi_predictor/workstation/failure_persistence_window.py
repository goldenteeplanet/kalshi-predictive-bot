from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .failure_observation_quorum import (
    FailureQuorumDecision,
    validate_failure_quorum_decision,
)

PERSISTENCE_SCHEMA_VERSION = "phase4hx-failure-persistence-window-v1"
PersistenceStatus = Literal["PERSISTENT", "TRANSIENT", "INCOMPLETE", "TAMPERED", "DENIED"]


class FailurePersistenceWindowError(ValueError):
    """Stable fail-closed failure persistence window error."""


@dataclass(frozen=True)
class FailurePersistenceDecision:
    status: PersistenceStatus
    reasons: tuple[str, ...]
    dependency_code: str
    evaluated_at_epoch_seconds: int
    minimum_persistence_seconds: int
    maximum_latest_age_seconds: int
    decision_count: int
    first_quorum_at_epoch_seconds: int | None
    last_quorum_at_epoch_seconds: int | None
    observed_span_seconds: int
    quorum_history_hash: str
    decision_hash: str
    read_only: bool = True
    failure_persistence_proven: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def evaluate_failure_persistence_window(
    decisions: Sequence[Any],
    *,
    dependency_code: str,
    evaluated_at_epoch_seconds: int,
    minimum_persistence_seconds: int = 60,
    maximum_latest_age_seconds: int = 120,
    max_records: int = 32,
) -> FailurePersistenceDecision:
    if not isinstance(dependency_code, str) or not dependency_code:
        raise FailurePersistenceWindowError("PERSISTENCE_DEPENDENCY_INVALID")
    for value in (
        evaluated_at_epoch_seconds,
        minimum_persistence_seconds,
        maximum_latest_age_seconds,
        max_records,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FailurePersistenceWindowError("PERSISTENCE_BOUND_INVALID")
    if isinstance(decisions, (str, bytes)) or len(decisions) > max_records:
        raise FailurePersistenceWindowError("PERSISTENCE_RECORD_BOUND_EXCEEDED")
    records: list[FailureQuorumDecision] = []
    for item in decisions:
        if not isinstance(item, FailureQuorumDecision):
            raise FailurePersistenceWindowError("PERSISTENCE_DECISION_TYPE_INVALID")
        try:
            validate_failure_quorum_decision(item)
        except ValueError as exc:
            raise FailurePersistenceWindowError("PERSISTENCE_DECISION_INVALID") from exc
        records.append(item)
    records.sort(key=lambda item: (item.evaluated_at_epoch_seconds, item.decision_hash))
    hashes = [item.decision_hash for item in records]
    mixed = any(item.dependency_code != dependency_code for item in records)
    future = any(item.evaluated_at_epoch_seconds > evaluated_at_epoch_seconds for item in records)
    quorum = [item for item in records if item.status == "QUORUM"]
    first = quorum[0].evaluated_at_epoch_seconds if quorum else None
    last = quorum[-1].evaluated_at_epoch_seconds if quorum else None
    span = 0 if first is None or last is None else last - first
    if len(set(hashes)) != len(hashes) or mixed:
        status: PersistenceStatus = "TAMPERED"
        reasons = ["PERSISTENCE_HISTORY_DUPLICATE_OR_DEPENDENCY_MISMATCH"]
    elif future:
        status = "DENIED"
        reasons = ["PERSISTENCE_DECISION_FROM_FUTURE"]
    elif any(item.status in {"INCOMPLETE", "TAMPERED", "DENIED"} for item in records):
        status = "INCOMPLETE"
        reasons = ["PERSISTENCE_HISTORY_NOT_PROVEN"]
    elif len(quorum) < 2:
        status = "TRANSIENT"
        reasons = ["PERSISTENCE_REQUIRES_MULTIPLE_QUORUM_DECISIONS"]
    elif evaluated_at_epoch_seconds - last > maximum_latest_age_seconds:
        status = "TRANSIENT"
        reasons = ["PERSISTENCE_LATEST_QUORUM_STALE"]
    elif span < minimum_persistence_seconds:
        status = "TRANSIENT"
        reasons = ["PERSISTENCE_WINDOW_NOT_MET"]
    else:
        status = "PERSISTENT"
        reasons = []
    proven = status == "PERSISTENT"
    history_hash = _hash([asdict(item) for item in records])
    unsigned = {
        "schema_version": PERSISTENCE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "dependency_code": dependency_code,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "minimum_persistence_seconds": minimum_persistence_seconds,
        "maximum_latest_age_seconds": maximum_latest_age_seconds,
        "decision_count": len(records),
        "first_quorum_at_epoch_seconds": first,
        "last_quorum_at_epoch_seconds": last,
        "observed_span_seconds": span,
        "quorum_history_hash": history_hash,
        "read_only": True,
        "failure_persistence_proven": proven,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return FailurePersistenceDecision(
        status=status,
        reasons=tuple(reasons),
        dependency_code=dependency_code,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        minimum_persistence_seconds=minimum_persistence_seconds,
        maximum_latest_age_seconds=maximum_latest_age_seconds,
        decision_count=len(records),
        first_quorum_at_epoch_seconds=first,
        last_quorum_at_epoch_seconds=last,
        observed_span_seconds=span,
        quorum_history_hash=history_hash,
        decision_hash=_hash(unsigned),
        failure_persistence_proven=proven,
    )


def validate_failure_persistence_decision(value: Any) -> None:
    if not isinstance(value, FailurePersistenceDecision):
        raise FailurePersistenceWindowError("PERSISTENCE_RESULT_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise FailurePersistenceWindowError("PERSISTENCE_SAFETY_BOUNDARY_INVALID")
    if value.failure_persistence_proven != (value.status == "PERSISTENT"):
        raise FailurePersistenceWindowError("PERSISTENCE_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = PERSISTENCE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise FailurePersistenceWindowError("PERSISTENCE_RESULT_HASH_MISMATCH")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
