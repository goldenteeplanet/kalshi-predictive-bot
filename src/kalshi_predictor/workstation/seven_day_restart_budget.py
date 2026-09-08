from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

BUDGET_SCHEMA_VERSION = "phase4jf-seven-day-restart-budget-v1"
WINDOW_SECONDS = 7 * 24 * 60 * 60
MAX_RESTARTS_PER_WINDOW = 2
BudgetStatus = Literal["AVAILABLE", "EXHAUSTED", "DENIED", "TAMPERED"]


class SevenDayRestartBudgetError(ValueError):
    """Stable fail-closed seven-day restart budget error."""


@dataclass(frozen=True)
class RestartBudgetState:
    state_id_hash: str
    history_file_hash: str
    restart_epochs: tuple[int, ...]
    state_complete: bool
    integrity_verified: bool
    state_hash: str


@dataclass(frozen=True)
class RestartBudgetDecision:
    status: BudgetStatus
    reasons: tuple[str, ...]
    state_hash: str
    evaluated_at_epoch: int
    window_start_epoch: int
    window_seconds: int
    maximum_restarts: int
    restarts_in_window: int
    remaining_restarts: int
    in_window_epochs: tuple[int, ...]
    decision_hash: str
    read_only: bool = True
    budget_available: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_budget_state(**fields: Any) -> RestartBudgetState:
    normalized = dict(fields)
    if isinstance(normalized.get("restart_epochs"), list):
        normalized["restart_epochs"] = tuple(normalized["restart_epochs"])
    _validate_fields(normalized)
    normalized["restart_epochs"] = tuple(sorted(normalized["restart_epochs"]))
    unsigned = {**normalized, "restart_epochs": list(normalized["restart_epochs"])}
    return RestartBudgetState(**normalized, state_hash=_hash(unsigned))


def evaluate_seven_day_restart_budget(
    state: Any, *, evaluated_at_epoch: int, max_history_records: int = 1_000
) -> RestartBudgetDecision:
    if (
        isinstance(evaluated_at_epoch, bool)
        or not isinstance(evaluated_at_epoch, int)
        or evaluated_at_epoch < 0
    ):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_TIME_INVALID")
    if (
        isinstance(max_history_records, bool)
        or not isinstance(max_history_records, int)
        or max_history_records <= 0
    ):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_BOUND_INVALID")
    item = _validated_state(state)
    if len(item.restart_epochs) > max_history_records:
        raise SevenDayRestartBudgetError("RESTART_BUDGET_HISTORY_BOUND_EXCEEDED")
    duplicates = len(set(item.restart_epochs)) != len(item.restart_epochs)
    future = any(epoch > evaluated_at_epoch for epoch in item.restart_epochs)
    window_start = max(0, evaluated_at_epoch - WINDOW_SECONDS)
    in_window = tuple(sorted(epoch for epoch in item.restart_epochs if epoch > window_start))
    if not item.state_complete or not item.integrity_verified:
        status: BudgetStatus = "DENIED"
        reasons = ["RESTART_BUDGET_STATE_UNTRUSTED"]
    elif duplicates or future:
        status = "TAMPERED"
        reasons = []
        if duplicates:
            reasons.append("RESTART_BUDGET_DUPLICATE_HISTORY")
        if future:
            reasons.append("RESTART_BUDGET_FUTURE_HISTORY")
    elif len(in_window) >= MAX_RESTARTS_PER_WINDOW:
        status = "EXHAUSTED"
        reasons = ["RESTART_BUDGET_EXHAUSTED"]
    else:
        status = "AVAILABLE"
        reasons = []
    remaining = max(0, MAX_RESTARTS_PER_WINDOW - len(in_window))
    available = status == "AVAILABLE"
    unsigned = {
        "schema_version": BUDGET_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "state_hash": item.state_hash,
        "evaluated_at_epoch": evaluated_at_epoch,
        "window_start_epoch": window_start,
        "window_seconds": WINDOW_SECONDS,
        "maximum_restarts": MAX_RESTARTS_PER_WINDOW,
        "restarts_in_window": len(in_window),
        "remaining_restarts": remaining,
        "in_window_epochs": list(in_window),
        "read_only": True,
        "budget_available": available,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartBudgetDecision(
        status=status,
        reasons=tuple(reasons),
        state_hash=item.state_hash,
        evaluated_at_epoch=evaluated_at_epoch,
        window_start_epoch=window_start,
        window_seconds=WINDOW_SECONDS,
        maximum_restarts=MAX_RESTARTS_PER_WINDOW,
        restarts_in_window=len(in_window),
        remaining_restarts=remaining,
        in_window_epochs=in_window,
        decision_hash=_hash(unsigned),
        budget_available=available,
    )


def validate_restart_budget_decision(value: Any) -> None:
    if not isinstance(value, RestartBudgetDecision):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_DECISION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.window_seconds != WINDOW_SECONDS
        or value.maximum_restarts != MAX_RESTARTS_PER_WINDOW
        or any(
            (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
        )
    ):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_SAFETY_BOUNDARY_INVALID")
    if value.budget_available != (value.status == "AVAILABLE"):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = BUDGET_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["in_window_epochs"] = list(unsigned["in_window_epochs"])
    if value.decision_hash != _hash(unsigned):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_DECISION_HASH_MISMATCH")


def _validated_state(value: Any) -> RestartBudgetState:
    if not isinstance(value, RestartBudgetState):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_STATE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("state_hash")
    unsigned["restart_epochs"] = tuple(unsigned["restart_epochs"])
    _validate_fields(unsigned)
    unsigned["restart_epochs"] = list(unsigned["restart_epochs"])
    if supplied != _hash(unsigned):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_STATE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "state_id_hash",
        "history_file_hash",
        "restart_epochs",
        "state_complete",
        "integrity_verified",
    }
    if set(fields) != required:
        raise SevenDayRestartBudgetError("RESTART_BUDGET_STATE_FIELD_INVALID")
    for key in ("state_id_hash", "history_file_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SevenDayRestartBudgetError("RESTART_BUDGET_STATE_FIELD_INVALID")
    if not isinstance(fields["restart_epochs"], tuple) or any(
        isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0
        for epoch in fields["restart_epochs"]
    ):
        raise SevenDayRestartBudgetError("RESTART_BUDGET_STATE_FIELD_INVALID")
    for key in ("state_complete", "integrity_verified"):
        if not isinstance(fields[key], bool):
            raise SevenDayRestartBudgetError("RESTART_BUDGET_STATE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
