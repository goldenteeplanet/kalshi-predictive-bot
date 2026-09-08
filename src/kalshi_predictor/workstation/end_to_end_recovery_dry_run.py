from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

REHEARSAL_SCHEMA_VERSION = "phase4kn-end-to-end-recovery-dry-run-v1"
REQUIRED_STEPS = (
    "DETECT_FAILURE",
    "SEND_ALERT",
    "ATTEMPT_COMPONENT_RECOVERY",
    "EVALUATE_RESTART_ELIGIBILITY",
    "ISSUE_RESTART_WARNING",
    "CHECK_CANCELLATION",
    "RECORD_RESTART_INTENT",
    "SIMULATE_NON_FORCED_RESTART",
    "VERIFY_POST_BOOT_CHAIN",
    "BUILD_OPERATOR_HANDOFF",
)
StepStatus = Literal["PASS", "FAIL", "SKIP"]
RehearsalStatus = Literal["PASSED", "FAILED", "INCOMPLETE"]


class EndToEndRecoveryDryRunError(ValueError):
    """Stable fail-closed end-to-end dry-run error."""


@dataclass(frozen=True)
class DryRunStep:
    name: str
    status: StepStatus
    evidence_hash: str
    dry_run: bool
    side_effect_observed: bool


@dataclass(frozen=True)
class EndToEndDryRunReport:
    status: RehearsalStatus
    reasons: tuple[str, ...]
    steps: tuple[DryRunStep, ...]
    first_failed_step: str | None
    report_hash: str
    dry_run_only: bool = True
    fail_stop_proven: bool = False
    side_effect_free: bool = False
    recovery_authorized: bool = False
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_dry_run_step(
    *,
    name: str,
    status: StepStatus,
    evidence_hash: str,
    dry_run: bool = True,
    side_effect_observed: bool = False,
) -> DryRunStep:
    if name not in REQUIRED_STEPS or status not in {"PASS", "FAIL", "SKIP"}:
        raise EndToEndRecoveryDryRunError("DRY_RUN_STEP_FIELD_INVALID")
    if not isinstance(evidence_hash, str) or re.fullmatch(r"[0-9a-f]{64}", evidence_hash) is None:
        raise EndToEndRecoveryDryRunError("DRY_RUN_STEP_FIELD_INVALID")
    if not isinstance(dry_run, bool) or not isinstance(side_effect_observed, bool):
        raise EndToEndRecoveryDryRunError("DRY_RUN_STEP_FIELD_INVALID")
    return DryRunStep(name, status, evidence_hash, dry_run, side_effect_observed)


def evaluate_end_to_end_recovery_dry_run(steps: Any) -> EndToEndDryRunReport:
    if not isinstance(steps, tuple) or any(not isinstance(item, DryRunStep) for item in steps):
        raise EndToEndRecoveryDryRunError("DRY_RUN_STEPS_TYPE_INVALID")
    names = tuple(item.name for item in steps)
    reasons = []
    if names != REQUIRED_STEPS:
        status: RehearsalStatus = "INCOMPLETE"
        reasons.append("DRY_RUN_STEP_COVERAGE_OR_ORDER_INVALID")
    else:
        status = "PASSED"
    side_effect_free = all(item.dry_run and not item.side_effect_observed for item in steps)
    if not side_effect_free:
        status = "FAILED"
        reasons.append("DRY_RUN_SIDE_EFFECT_BOUNDARY_VIOLATED")
    failed_indexes = [index for index, item in enumerate(steps) if item.status == "FAIL"]
    first_failed = steps[failed_indexes[0]].name if failed_indexes else None
    fail_stop = True
    if failed_indexes:
        first_index = failed_indexes[0]
        fail_stop = all(item.status == "SKIP" for item in steps[first_index + 1 :])
        if not fail_stop:
            status = "FAILED"
            reasons.append("DRY_RUN_FAIL_STOP_VIOLATED")
        elif status != "INCOMPLETE":
            status = "FAILED"
            reasons.append(f"DRY_RUN_STEP_FAILED:{first_failed}")
    elif any(item.status != "PASS" for item in steps) and status != "INCOMPLETE":
        status = "INCOMPLETE"
        reasons.append("DRY_RUN_UNEXPLAINED_SKIP")
        fail_stop = False
    unsigned = {
        "schema_version": REHEARSAL_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "steps": [asdict(item) for item in steps],
        "first_failed_step": first_failed,
        "dry_run_only": True,
        "fail_stop_proven": fail_stop,
        "side_effect_free": side_effect_free,
        "recovery_authorized": False,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return EndToEndDryRunReport(
        status=status,
        reasons=tuple(reasons),
        steps=steps,
        first_failed_step=first_failed,
        report_hash=_hash(unsigned),
        fail_stop_proven=fail_stop,
        side_effect_free=side_effect_free,
    )


def validate_end_to_end_dry_run_report(value: Any) -> None:
    if not isinstance(value, EndToEndDryRunReport):
        raise EndToEndRecoveryDryRunError("DRY_RUN_REPORT_TYPE_INVALID")
    if value.dry_run_only is not True or any(
        (
            value.recovery_authorized,
            value.restart_authorized,
            value.service_control_authorized,
            value.execution_authorized,
        )
    ):
        raise EndToEndRecoveryDryRunError("DRY_RUN_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("report_hash")
    unsigned["schema_version"] = REHEARSAL_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["steps"] = [asdict(item) for item in value.steps]
    if value.report_hash != _hash(unsigned):
        raise EndToEndRecoveryDryRunError("DRY_RUN_REPORT_HASH_MISMATCH")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
