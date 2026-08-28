from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

DRILL_SCHEMA_VERSION = "phase4ku-operator-recovery-drill-v1"
REQUIRED_STEPS = (
    "ACKNOWLEDGE_ALERT",
    "VERIFY_INCIDENT_IDENTITY",
    "INSPECT_READ_ONLY_EVIDENCE",
    "CONFIRM_PAPER_ONLY_BOUNDARY",
    "REVIEW_COMPONENT_RECOVERY_RESULT",
    "REVIEW_RESTART_WARNING_AND_CANCELLATION",
    "VERIFY_COOLDOWN_BUDGET_AND_LOOP_BREAKER",
    "REVIEW_MOCKED_RESTART_RESULT",
    "VERIFY_POST_BOOT_CHAIN",
    "QUARANTINE_AUTOMATION_ON_FAILURE",
    "REVIEW_PROTECTED_INVARIANTS",
    "COMPLETE_OPERATOR_HANDOFF",
)
DrillStatus = Literal["PASSED", "FAILED", "INCOMPLETE", "TAMPERED"]


class OperatorRecoveryDrillError(ValueError):
    """Stable fail-closed operator recovery drill error."""


@dataclass(frozen=True)
class OperatorDrillStep:
    name: str
    completed_at_epoch_seconds: int
    evidence_hash: str
    completed: bool
    simulated_only: bool


@dataclass(frozen=True)
class OperatorRecoveryDrillResult:
    status: DrillStatus
    reasons: tuple[str, ...]
    steps_hash: str
    completed_steps: int
    result_hash: str
    ordered_execution_proven: bool = False
    paper_only_proven: bool = False
    operator_handoff_proven: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    trading_authorized: bool = False
    execution_authorized: bool = False


def make_operator_drill_step(
    *,
    name: str,
    completed_at_epoch_seconds: int,
    evidence_hash: str,
    completed: bool = True,
    simulated_only: bool = True,
) -> OperatorDrillStep:
    if (
        name not in REQUIRED_STEPS
        or isinstance(completed_at_epoch_seconds, bool)
        or not isinstance(completed_at_epoch_seconds, int)
        or completed_at_epoch_seconds < 0
        or not isinstance(evidence_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence_hash) is None
        or not isinstance(completed, bool)
        or not isinstance(simulated_only, bool)
    ):
        raise OperatorRecoveryDrillError("OPERATOR_DRILL_STEP_FIELD_INVALID")
    return OperatorDrillStep(
        name, completed_at_epoch_seconds, evidence_hash, completed, simulated_only
    )


def evaluate_operator_recovery_drill(steps: Any) -> OperatorRecoveryDrillResult:
    if not isinstance(steps, tuple) or any(
        not isinstance(item, OperatorDrillStep) for item in steps
    ):
        raise OperatorRecoveryDrillError("OPERATOR_DRILL_STEPS_TYPE_INVALID")
    names = tuple(item.name for item in steps)
    duplicate = len(set(names)) != len(names)
    timestamps = tuple(item.completed_at_epoch_seconds for item in steps)
    reasons = []
    if duplicate:
        status: DrillStatus = "TAMPERED"
        reasons.append("OPERATOR_DRILL_STEP_DUPLICATED")
    elif names != REQUIRED_STEPS:
        status = "INCOMPLETE"
        reasons.append("OPERATOR_DRILL_COVERAGE_OR_ORDER_INVALID")
    elif timestamps != tuple(sorted(timestamps)):
        status = "TAMPERED"
        reasons.append("OPERATOR_DRILL_TIME_ORDER_INVALID")
    else:
        failures = []
        if any(not item.completed for item in steps):
            failures.append("OPERATOR_DRILL_STEP_NOT_COMPLETED")
        if any(not item.simulated_only for item in steps):
            failures.append("OPERATOR_DRILL_LIVE_OPERATION_OBSERVED")
        status = "FAILED" if failures else "PASSED"
        reasons = failures
    passed = status == "PASSED"
    payload = [asdict(item) for item in steps]
    unsigned = {
        "schema_version": DRILL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "steps_hash": _hash(payload),
        "completed_steps": sum(item.completed for item in steps),
        "ordered_execution_proven": passed,
        "paper_only_proven": passed,
        "operator_handoff_proven": passed,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "trading_authorized": False,
        "execution_authorized": False,
    }
    return OperatorRecoveryDrillResult(
        status=status,
        reasons=tuple(reasons),
        steps_hash=unsigned["steps_hash"],
        completed_steps=unsigned["completed_steps"],
        result_hash=_hash(unsigned),
        ordered_execution_proven=passed,
        paper_only_proven=passed,
        operator_handoff_proven=passed,
    )


def validate_operator_recovery_drill_result(value: Any) -> None:
    if not isinstance(value, OperatorRecoveryDrillResult):
        raise OperatorRecoveryDrillError("OPERATOR_DRILL_RESULT_TYPE_INVALID")
    passed = value.status == "PASSED"
    if any(
        flag != passed
        for flag in (
            value.ordered_execution_proven,
            value.paper_only_proven,
            value.operator_handoff_proven,
        )
    ) or any(
        (
            value.recovery_authorized,
            value.restart_authorized,
            value.service_control_authorized,
            value.trading_authorized,
            value.execution_authorized,
        )
    ):
        raise OperatorRecoveryDrillError("OPERATOR_DRILL_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = DRILL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.result_hash != _hash(unsigned):
        raise OperatorRecoveryDrillError("OPERATOR_DRILL_RESULT_HASH_MISMATCH")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
