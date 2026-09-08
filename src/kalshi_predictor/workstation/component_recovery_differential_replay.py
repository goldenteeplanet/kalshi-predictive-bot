from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kalshi_predictor.utils.json_digest import json_value_digest as _hash

REPLAY_SCHEMA_VERSION = "phase4ix-component-recovery-differential-replay-v1"
ReplayStatus = Literal["MATCH", "DRIFT", "SAFETY_REGRESSION", "INCOMPLETE", "TAMPERED"]


class ComponentRecoveryDifferentialReplayError(ValueError):
    """Stable fail-closed component recovery differential replay error."""


@dataclass(frozen=True)
class RecoveryReplayCase:
    scenario_id_hash: str
    input_hash: str
    baseline_status: str
    baseline_output_hash: str
    baseline_safety_proven: bool
    candidate_status: str
    candidate_output_hash: str
    candidate_safety_proven: bool
    complete: bool
    case_hash: str


@dataclass(frozen=True)
class ComponentRecoveryReplayDecision:
    status: ReplayStatus
    reasons: tuple[str, ...]
    case_count: int
    matched_count: int
    drift_count: int
    safety_regression_count: int
    case_hashes: tuple[str, ...]
    replay_set_hash: str
    decision_hash: str
    read_only: bool = True
    differential_equivalence_proven: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_recovery_replay_case(**fields: Any) -> RecoveryReplayCase:
    _validate_fields(fields)
    return RecoveryReplayCase(**fields, case_hash=_hash(fields))


def evaluate_component_recovery_differential_replay(
    cases: Sequence[Any], *, max_cases: int = 128
) -> ComponentRecoveryReplayDecision:
    if isinstance(max_cases, bool) or not isinstance(max_cases, int) or max_cases <= 0:
        raise ComponentRecoveryDifferentialReplayError("REPLAY_BOUND_INVALID")
    if isinstance(cases, str | bytes) or len(cases) > max_cases:
        raise ComponentRecoveryDifferentialReplayError("REPLAY_CASE_BOUND_EXCEEDED")
    records = [_validated_case(item) for item in cases]
    records.sort(key=lambda item: item.scenario_id_hash)
    ids = [item.scenario_id_hash for item in records]
    safety_regressions = [
        item for item in records if item.baseline_safety_proven and not item.candidate_safety_proven
    ]
    drifts = [
        item
        for item in records
        if item.baseline_status != item.candidate_status
        or item.baseline_output_hash != item.candidate_output_hash
    ]
    matches = [item for item in records if item not in drifts and item.candidate_safety_proven]
    if len(set(ids)) != len(ids):
        status: ReplayStatus = "TAMPERED"
        reasons = ["REPLAY_SCENARIO_DUPLICATE"]
    elif not records or any(not item.complete for item in records):
        status = "INCOMPLETE"
        reasons = ["REPLAY_CASE_SET_EMPTY_OR_INCOMPLETE"]
    elif safety_regressions or any(not item.candidate_safety_proven for item in records):
        status = "SAFETY_REGRESSION"
        reasons = ["RECOVERY_REPLAY_SAFETY_REGRESSION"]
    elif drifts:
        status = "DRIFT"
        reasons = ["RECOVERY_REPLAY_OUTPUT_DRIFT"]
    else:
        status = "MATCH"
        reasons = []
    proven = status == "MATCH"
    hashes = tuple(item.case_hash for item in records)
    set_hash = _hash([asdict(item) for item in records])
    unsigned = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "case_count": len(records),
        "matched_count": len(matches),
        "drift_count": len(drifts),
        "safety_regression_count": len(safety_regressions),
        "case_hashes": list(hashes),
        "replay_set_hash": set_hash,
        "read_only": True,
        "differential_equivalence_proven": proven,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return ComponentRecoveryReplayDecision(
        status=status,
        reasons=tuple(reasons),
        case_count=len(records),
        matched_count=len(matches),
        drift_count=len(drifts),
        safety_regression_count=len(safety_regressions),
        case_hashes=hashes,
        replay_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        differential_equivalence_proven=proven,
    )


def validate_component_recovery_replay_decision(value: Any) -> None:
    if not isinstance(value, ComponentRecoveryReplayDecision):
        raise ComponentRecoveryDifferentialReplayError("REPLAY_DECISION_TYPE_INVALID")
    if value.read_only is not True or any(
        (
            value.recovery_authorized,
            value.service_control_authorized,
            value.host_restart_authorized,
            value.execution_authorized,
        )
    ):
        raise ComponentRecoveryDifferentialReplayError("REPLAY_SAFETY_BOUNDARY_INVALID")
    if value.differential_equivalence_proven != (value.status == "MATCH"):
        raise ComponentRecoveryDifferentialReplayError("REPLAY_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = REPLAY_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["case_hashes"] = list(unsigned["case_hashes"])
    if value.decision_hash != _hash(unsigned):
        raise ComponentRecoveryDifferentialReplayError("REPLAY_DECISION_HASH_MISMATCH")


def _validated_case(value: Any) -> RecoveryReplayCase:
    if not isinstance(value, RecoveryReplayCase):
        raise ComponentRecoveryDifferentialReplayError("REPLAY_CASE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("case_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise ComponentRecoveryDifferentialReplayError("REPLAY_CASE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "scenario_id_hash",
        "input_hash",
        "baseline_status",
        "baseline_output_hash",
        "baseline_safety_proven",
        "candidate_status",
        "candidate_output_hash",
        "candidate_safety_proven",
        "complete",
    }
    if set(fields) != required:
        raise ComponentRecoveryDifferentialReplayError("REPLAY_CASE_FIELD_INVALID")
    for key in ("scenario_id_hash", "input_hash", "baseline_output_hash", "candidate_output_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise ComponentRecoveryDifferentialReplayError("REPLAY_CASE_FIELD_INVALID")
    for key in ("baseline_status", "candidate_status"):
        if (
            not isinstance(fields[key], str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields[key]) is None
        ):
            raise ComponentRecoveryDifferentialReplayError("REPLAY_CASE_FIELD_INVALID")
    for key in ("baseline_safety_proven", "candidate_safety_proven", "complete"):
        if not isinstance(fields[key], bool):
            raise ComponentRecoveryDifferentialReplayError("REPLAY_CASE_FIELD_INVALID")
