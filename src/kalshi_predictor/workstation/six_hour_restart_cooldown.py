from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

COOLDOWN_SCHEMA_VERSION = "phase4je-six-hour-restart-cooldown-v1"
COOLDOWN_SECONDS = 6 * 60 * 60
CooldownStatus = Literal["CLEAR", "ACTIVE", "DENIED", "TAMPERED"]


class SixHourRestartCooldownError(ValueError):
    """Stable fail-closed six-hour restart cooldown error."""


@dataclass(frozen=True)
class RestartCooldownState:
    state_id_hash: str
    history_file_hash: str
    latest_intent_hash: str
    has_restart_history: bool
    last_restart_at_epoch: int
    state_complete: bool
    integrity_verified: bool
    state_hash: str


@dataclass(frozen=True)
class RestartCooldownDecision:
    status: CooldownStatus
    reasons: tuple[str, ...]
    state_hash: str
    evaluated_at_epoch: int
    cooldown_seconds: int
    elapsed_seconds: int
    remaining_seconds: int
    next_eligible_at_epoch: int
    decision_hash: str
    read_only: bool = True
    cooldown_clear: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_cooldown_state(**fields: Any) -> RestartCooldownState:
    _validate_fields(fields)
    return RestartCooldownState(**fields, state_hash=_hash(fields))


def evaluate_six_hour_restart_cooldown(
    state: Any, *, evaluated_at_epoch: int
) -> RestartCooldownDecision:
    if (
        isinstance(evaluated_at_epoch, bool)
        or not isinstance(evaluated_at_epoch, int)
        or evaluated_at_epoch < 0
    ):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_TIME_INVALID")
    item = _validated_state(state)
    if not item.state_complete or not item.integrity_verified:
        status: CooldownStatus = "DENIED"
        reasons = ["RESTART_COOLDOWN_STATE_UNTRUSTED"]
    elif not item.has_restart_history and item.last_restart_at_epoch != 0:
        status = "TAMPERED"
        reasons = ["RESTART_COOLDOWN_EMPTY_HISTORY_CONTRADICTION"]
    elif item.has_restart_history and item.last_restart_at_epoch == 0:
        status = "TAMPERED"
        reasons = ["RESTART_COOLDOWN_HISTORY_TIMESTAMP_MISSING"]
    elif item.last_restart_at_epoch > evaluated_at_epoch:
        status = "TAMPERED"
        reasons = ["RESTART_COOLDOWN_FUTURE_HISTORY"]
    elif not item.has_restart_history:
        status = "CLEAR"
        reasons = []
    elif evaluated_at_epoch - item.last_restart_at_epoch < COOLDOWN_SECONDS:
        status = "ACTIVE"
        reasons = ["RESTART_COOLDOWN_ACTIVE"]
    else:
        status = "CLEAR"
        reasons = []
    elapsed = (
        evaluated_at_epoch - item.last_restart_at_epoch
        if item.has_restart_history and item.last_restart_at_epoch <= evaluated_at_epoch
        else 0
    )
    remaining = max(0, COOLDOWN_SECONDS - elapsed) if item.has_restart_history else 0
    next_eligible = (
        item.last_restart_at_epoch + COOLDOWN_SECONDS
        if item.has_restart_history
        else evaluated_at_epoch
    )
    clear = status == "CLEAR"
    unsigned = {
        "schema_version": COOLDOWN_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "state_hash": item.state_hash,
        "evaluated_at_epoch": evaluated_at_epoch,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "next_eligible_at_epoch": next_eligible,
        "read_only": True,
        "cooldown_clear": clear,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartCooldownDecision(
        status=status,
        reasons=tuple(reasons),
        state_hash=item.state_hash,
        evaluated_at_epoch=evaluated_at_epoch,
        cooldown_seconds=COOLDOWN_SECONDS,
        elapsed_seconds=elapsed,
        remaining_seconds=remaining,
        next_eligible_at_epoch=next_eligible,
        decision_hash=_hash(unsigned),
        cooldown_clear=clear,
    )


def validate_restart_cooldown_decision(value: Any) -> None:
    if not isinstance(value, RestartCooldownDecision):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.cooldown_seconds != COOLDOWN_SECONDS
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_SAFETY_BOUNDARY_INVALID")
    if value.cooldown_clear != (value.status == "CLEAR"):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = COOLDOWN_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.decision_hash != _hash(unsigned):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_DECISION_HASH_MISMATCH")


def _validated_state(value: Any) -> RestartCooldownState:
    if not isinstance(value, RestartCooldownState):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_STATE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("state_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_STATE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "state_id_hash",
        "history_file_hash",
        "latest_intent_hash",
        "has_restart_history",
        "last_restart_at_epoch",
        "state_complete",
        "integrity_verified",
    }
    if set(fields) != required:
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_STATE_FIELD_INVALID")
    for key in ("state_id_hash", "history_file_hash", "latest_intent_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SixHourRestartCooldownError("RESTART_COOLDOWN_STATE_FIELD_INVALID")
    if (
        isinstance(fields["last_restart_at_epoch"], bool)
        or not isinstance(fields["last_restart_at_epoch"], int)
        or fields["last_restart_at_epoch"] < 0
    ):
        raise SixHourRestartCooldownError("RESTART_COOLDOWN_STATE_FIELD_INVALID")
    for key in ("has_restart_history", "state_complete", "integrity_verified"):
        if not isinstance(fields[key], bool):
            raise SixHourRestartCooldownError("RESTART_COOLDOWN_STATE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
