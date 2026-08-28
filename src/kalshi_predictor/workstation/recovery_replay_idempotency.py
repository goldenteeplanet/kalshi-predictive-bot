from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

VERIFICATION_SCHEMA_VERSION = "phase4jr-recovery-replay-idempotency-v1"
VerificationStatus = Literal["PASS", "FAIL", "INCOMPLETE", "TAMPERED"]


class RecoveryReplayIdempotencyError(ValueError):
    """Stable fail-closed recovery replay idempotency error."""


@dataclass(frozen=True)
class ReplayIdempotencyCase:
    scenario_id_hash: str
    input_hash: str
    first_result_hash: str
    replay_result_hash: str
    first_state_hash: str
    replay_state_hash: str
    initial_effect_count: int
    first_effect_count: int
    replay_effect_count: int
    complete: bool
    case_hash: str


@dataclass(frozen=True)
class ReplayIdempotencyDecision:
    status: VerificationStatus
    reasons: tuple[str, ...]
    case_count: int
    idempotent_count: int
    divergent_count: int
    case_hashes: tuple[str, ...]
    verification_set_hash: str
    decision_hash: str
    read_only: bool = True
    replay_idempotency_proven: bool = False
    additional_effects_permitted: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_replay_idempotency_case(**fields: Any) -> ReplayIdempotencyCase:
    _validate_fields(fields)
    return ReplayIdempotencyCase(**fields, case_hash=_hash(fields))


def verify_recovery_replay_idempotency(
    cases: Sequence[Any], *, max_cases: int = 128
) -> ReplayIdempotencyDecision:
    if isinstance(max_cases, bool) or not isinstance(max_cases, int) or max_cases <= 0:
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_BOUND_INVALID")
    if isinstance(cases, (str, bytes)) or len(cases) > max_cases:
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_CASE_BOUND_EXCEEDED")
    records = sorted(
        (_validated_case(item) for item in cases), key=lambda item: item.scenario_id_hash
    )
    ids = [item.scenario_id_hash for item in records]
    duplicates = len(set(ids)) != len(ids)
    incomplete = [item for item in records if not item.complete]
    divergent = [
        item
        for item in records
        if item.first_result_hash != item.replay_result_hash
        or item.first_state_hash != item.replay_state_hash
        or item.first_effect_count != item.replay_effect_count
        or item.first_effect_count < item.initial_effect_count
        or item.first_effect_count - item.initial_effect_count > 1
    ]
    if duplicates:
        status: VerificationStatus = "TAMPERED"
        reasons = ["REPLAY_IDEMPOTENCY_SCENARIO_DUPLICATE"]
    elif not records or incomplete:
        status = "INCOMPLETE"
        reasons = (
            ["REPLAY_IDEMPOTENCY_CASES_EMPTY"]
            if not records
            else [
                f"REPLAY_IDEMPOTENCY_CASE_INCOMPLETE:{item.scenario_id_hash}" for item in incomplete
            ]
        )
    elif divergent:
        status = "FAIL"
        reasons = [f"REPLAY_IDEMPOTENCY_DIVERGENCE:{item.scenario_id_hash}" for item in divergent]
    else:
        status = "PASS"
        reasons = []
    passed = status == "PASS"
    case_hashes = tuple(item.case_hash for item in records)
    set_hash = _hash([asdict(item) for item in records])
    unsigned = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "case_count": len(records),
        "idempotent_count": len(records) - len(divergent),
        "divergent_count": len(divergent),
        "case_hashes": list(case_hashes),
        "verification_set_hash": set_hash,
        "read_only": True,
        "replay_idempotency_proven": passed,
        "additional_effects_permitted": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return ReplayIdempotencyDecision(
        status=status,
        reasons=tuple(reasons),
        case_count=len(records),
        idempotent_count=len(records) - len(divergent),
        divergent_count=len(divergent),
        case_hashes=case_hashes,
        verification_set_hash=set_hash,
        decision_hash=_hash(unsigned),
        replay_idempotency_proven=passed,
    )


def validate_replay_idempotency_decision(value: Any) -> None:
    if not isinstance(value, ReplayIdempotencyDecision):
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.replay_idempotency_proven != (value.status == "PASS")
        or value.additional_effects_permitted is not False
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = VERIFICATION_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["case_hashes"] = list(unsigned["case_hashes"])
    if value.decision_hash != _hash(unsigned):
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_DECISION_HASH_MISMATCH")


def _validated_case(value: Any) -> ReplayIdempotencyCase:
    if not isinstance(value, ReplayIdempotencyCase):
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_CASE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("case_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_CASE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "scenario_id_hash",
        "input_hash",
        "first_result_hash",
        "replay_result_hash",
        "first_state_hash",
        "replay_state_hash",
        "initial_effect_count",
        "first_effect_count",
        "replay_effect_count",
        "complete",
    }
    if set(fields) != required:
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_CASE_FIELD_INVALID")
    for key in (
        "scenario_id_hash",
        "input_hash",
        "first_result_hash",
        "replay_result_hash",
        "first_state_hash",
        "replay_state_hash",
    ):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_CASE_FIELD_INVALID")
    for key in ("initial_effect_count", "first_effect_count", "replay_effect_count"):
        if (
            isinstance(fields[key], bool)
            or not isinstance(fields[key], int)
            or not 0 <= fields[key] <= 10_000
        ):
            raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_CASE_FIELD_INVALID")
    if not isinstance(fields["complete"], bool):
        raise RecoveryReplayIdempotencyError("REPLAY_IDEMPOTENCY_CASE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
