from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .critical_dependency_allowlist import (
    CriticalDependencyDecision,
    validate_critical_dependency_decision,
)

QUORUM_SCHEMA_VERSION = "phase4hw-failure-observation-quorum-v1"
QuorumStatus = Literal["QUORUM", "NO_QUORUM", "INCOMPLETE", "TAMPERED", "DENIED"]


class FailureObservationQuorumError(ValueError):
    """Stable fail-closed failure observation quorum error."""


@dataclass(frozen=True)
class FailureObservation:
    observation_id_hash: str
    source_code: str
    dependency_code: str
    observed_at_epoch_seconds: int
    failure_observed: bool
    complete: bool
    observation_hash: str


@dataclass(frozen=True)
class FailureQuorumDecision:
    status: QuorumStatus
    reasons: tuple[str, ...]
    dependency_code: str
    allowlist_decision_hash: str
    evaluated_at_epoch_seconds: int
    freshness_window_seconds: int
    required_distinct_sources: int
    distinct_failure_sources: int
    observation_count: int
    observation_set_hash: str
    decision_hash: str
    read_only: bool = True
    failure_quorum_proven: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_failure_observation(
    *,
    observation_id_hash: str,
    source_code: str,
    dependency_code: str,
    observed_at_epoch_seconds: int,
    failure_observed: bool,
    complete: bool,
) -> FailureObservation:
    unsigned = {
        "observation_id_hash": observation_id_hash,
        "source_code": source_code,
        "dependency_code": dependency_code,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "failure_observed": failure_observed,
        "complete": complete,
    }
    _validate_fields(unsigned)
    return FailureObservation(**unsigned, observation_hash=_hash(unsigned))


def evaluate_failure_observation_quorum(
    allowlist_decision: Any,
    observations: Sequence[Any],
    *,
    evaluated_at_epoch_seconds: int,
    freshness_window_seconds: int = 120,
    required_distinct_sources: int = 2,
    max_records: int = 32,
) -> FailureQuorumDecision:
    for value in (
        evaluated_at_epoch_seconds,
        freshness_window_seconds,
        required_distinct_sources,
        max_records,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FailureObservationQuorumError("QUORUM_BOUND_INVALID")
    if required_distinct_sources > max_records:
        raise FailureObservationQuorumError("QUORUM_BOUND_INVALID")
    if not isinstance(allowlist_decision, CriticalDependencyDecision):
        raise FailureObservationQuorumError("ALLOWLIST_DECISION_TYPE_INVALID")
    try:
        validate_critical_dependency_decision(allowlist_decision)
    except ValueError as exc:
        raise FailureObservationQuorumError("ALLOWLIST_DECISION_INVALID") from exc
    if isinstance(observations, str | bytes) or len(observations) > max_records:
        raise FailureObservationQuorumError("QUORUM_RECORD_BOUND_EXCEEDED")
    records = [_validated_observation(item) for item in observations]
    records.sort(
        key=lambda item: (
            item.observed_at_epoch_seconds,
            item.source_code,
            item.observation_id_hash,
        )
    )
    ids = [item.observation_id_hash for item in records]
    mixed = any(item.dependency_code != allowlist_decision.dependency_code for item in records)
    future = any(item.observed_at_epoch_seconds > evaluated_at_epoch_seconds for item in records)
    stale = [
        item
        for item in records
        if evaluated_at_epoch_seconds - item.observed_at_epoch_seconds > freshness_window_seconds
    ]
    failure_sources = {item.source_code for item in records if item.failure_observed}
    if len(set(ids)) != len(ids) or mixed:
        status: QuorumStatus = "TAMPERED"
        reasons = ["QUORUM_OBSERVATION_DUPLICATE_OR_DEPENDENCY_MISMATCH"]
    elif allowlist_decision.status != "ALLOWLISTED":
        status = "DENIED"
        reasons = [f"DEPENDENCY_NOT_ALLOWLISTED:{allowlist_decision.status}"]
    elif future:
        status = "DENIED"
        reasons = ["QUORUM_OBSERVATION_FROM_FUTURE"]
    elif any(not item.complete for item in records):
        status = "INCOMPLETE"
        reasons = ["QUORUM_OBSERVATION_INCOMPLETE"]
    elif stale:
        status = "NO_QUORUM"
        reasons = ["QUORUM_OBSERVATION_STALE"]
    elif len(failure_sources) < required_distinct_sources:
        status = "NO_QUORUM"
        reasons = ["DISTINCT_FAILURE_SOURCE_QUORUM_NOT_MET"]
    else:
        status = "QUORUM"
        reasons = []
    proven = status == "QUORUM"
    set_hash = _hash([asdict(item) for item in records])
    unsigned = {
        "schema_version": QUORUM_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "dependency_code": allowlist_decision.dependency_code,
        "allowlist_decision_hash": allowlist_decision.decision_hash,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "freshness_window_seconds": freshness_window_seconds,
        "required_distinct_sources": required_distinct_sources,
        "distinct_failure_sources": len(failure_sources),
        "observation_count": len(records),
        "observation_set_hash": set_hash,
        "read_only": True,
        "failure_quorum_proven": proven,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return FailureQuorumDecision(
        status=status,
        reasons=tuple(reasons),
        dependency_code=allowlist_decision.dependency_code,
        allowlist_decision_hash=allowlist_decision.decision_hash,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        freshness_window_seconds=freshness_window_seconds,
        required_distinct_sources=required_distinct_sources,
        distinct_failure_sources=len(failure_sources),
        observation_count=len(records),
        observation_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        failure_quorum_proven=proven,
    )


def validate_failure_quorum_decision(value: Any) -> None:
    if not isinstance(value, FailureQuorumDecision):
        raise FailureObservationQuorumError("QUORUM_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise FailureObservationQuorumError("QUORUM_SAFETY_BOUNDARY_INVALID")
    if value.failure_quorum_proven != (value.status == "QUORUM"):
        raise FailureObservationQuorumError("QUORUM_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = QUORUM_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise FailureObservationQuorumError("QUORUM_DECISION_HASH_MISMATCH")


def _validated_observation(value: Any) -> FailureObservation:
    if not isinstance(value, FailureObservation):
        raise FailureObservationQuorumError("QUORUM_OBSERVATION_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("observation_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise FailureObservationQuorumError("QUORUM_OBSERVATION_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    if (
        not isinstance(fields["observation_id_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", fields["observation_id_hash"]) is None
    ):
        raise FailureObservationQuorumError("QUORUM_OBSERVATION_FIELD_INVALID")
    for key in ("source_code", "dependency_code"):
        if (
            not isinstance(fields[key], str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields[key]) is None
        ):
            raise FailureObservationQuorumError("QUORUM_OBSERVATION_FIELD_INVALID")
    timestamp = fields["observed_at_epoch_seconds"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise FailureObservationQuorumError("QUORUM_OBSERVATION_FIELD_INVALID")
    if not isinstance(fields["failure_observed"], bool) or not isinstance(fields["complete"], bool):
        raise FailureObservationQuorumError("QUORUM_OBSERVATION_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
