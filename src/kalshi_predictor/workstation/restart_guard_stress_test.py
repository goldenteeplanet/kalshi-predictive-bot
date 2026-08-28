from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

SCHEMA_VERSION = "phase4ks-restart-guard-stress-test-v1"
COOLDOWN_SECONDS = 21_600
MAX_RESTARTS_PER_SEVEN_DAYS = 2
Status = Literal["PASSED", "FAILED", "INCOMPLETE"]


class RestartGuardStressTestError(ValueError):
    """Stable fail-closed restart-guard stress-test error."""


@dataclass(frozen=True)
class RestartGuardTrial:
    cooldown_elapsed_seconds: int
    restarts_in_seven_days: int
    incident_restart_attempts: int
    observed_eligible: bool


@dataclass(frozen=True)
class RestartGuardStressReport:
    status: Status
    reasons: tuple[str, ...]
    trial_count: int
    mismatch_count: int
    trials_hash: str
    report_hash: str
    cooldown_boundaries_covered: bool = False
    budget_boundaries_covered: bool = False
    loop_breaker_boundaries_covered: bool = False
    read_only: bool = True
    restart_authorized: bool = False
    service_control_authorized: bool = False
    execution_authorized: bool = False


def make_restart_guard_trial(
    *,
    cooldown_elapsed_seconds: int,
    restarts_in_seven_days: int,
    incident_restart_attempts: int,
    observed_eligible: bool,
) -> RestartGuardTrial:
    for value in (cooldown_elapsed_seconds, restarts_in_seven_days, incident_restart_attempts):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RestartGuardStressTestError("RESTART_GUARD_TRIAL_FIELD_INVALID")
    if not isinstance(observed_eligible, bool):
        raise RestartGuardStressTestError("RESTART_GUARD_TRIAL_FIELD_INVALID")
    return RestartGuardTrial(
        cooldown_elapsed_seconds,
        restarts_in_seven_days,
        incident_restart_attempts,
        observed_eligible,
    )


def evaluate_restart_guard_stress_test(
    trials: Any, *, minimum_trials: int = 8
) -> RestartGuardStressReport:
    if not isinstance(trials, tuple) or any(
        not isinstance(item, RestartGuardTrial) for item in trials
    ):
        raise RestartGuardStressTestError("RESTART_GUARD_TRIALS_TYPE_INVALID")
    if (
        isinstance(minimum_trials, bool)
        or not isinstance(minimum_trials, int)
        or minimum_trials <= 0
    ):
        raise RestartGuardStressTestError("RESTART_GUARD_BOUND_INVALID")
    mismatches = []
    for index, item in enumerate(trials):
        expected = (
            item.cooldown_elapsed_seconds >= COOLDOWN_SECONDS
            and item.restarts_in_seven_days < MAX_RESTARTS_PER_SEVEN_DAYS
            and item.incident_restart_attempts == 0
        )
        if item.observed_eligible != expected:
            mismatches.append(index)
    cooldown_covered = {COOLDOWN_SECONDS - 1, COOLDOWN_SECONDS}.issubset(
        {item.cooldown_elapsed_seconds for item in trials}
    )
    budget_covered = {MAX_RESTARTS_PER_SEVEN_DAYS - 1, MAX_RESTARTS_PER_SEVEN_DAYS}.issubset(
        {item.restarts_in_seven_days for item in trials}
    )
    loop_covered = {0, 1}.issubset({item.incident_restart_attempts for item in trials})
    reasons = []
    if len(trials) < minimum_trials:
        reasons.append("RESTART_GUARD_STRESS_TRIAL_COUNT_INSUFFICIENT")
    if not cooldown_covered:
        reasons.append("RESTART_COOLDOWN_BOUNDARY_NOT_COVERED")
    if not budget_covered:
        reasons.append("RESTART_BUDGET_BOUNDARY_NOT_COVERED")
    if not loop_covered:
        reasons.append("RESTART_LOOP_BREAKER_BOUNDARY_NOT_COVERED")
    if mismatches:
        reasons.append("RESTART_GUARD_EXPECTATION_MISMATCH")
    status: Status = "PASSED" if not reasons else "FAILED" if mismatches else "INCOMPLETE"
    trials_payload = [asdict(item) for item in trials]
    unsigned = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "trial_count": len(trials),
        "mismatch_count": len(mismatches),
        "trials_hash": _hash(trials_payload),
        "cooldown_boundaries_covered": cooldown_covered,
        "budget_boundaries_covered": budget_covered,
        "loop_breaker_boundaries_covered": loop_covered,
        "read_only": True,
        "restart_authorized": False,
        "service_control_authorized": False,
        "execution_authorized": False,
    }
    return RestartGuardStressReport(
        status=status,
        reasons=tuple(reasons),
        trial_count=len(trials),
        mismatch_count=len(mismatches),
        trials_hash=unsigned["trials_hash"],
        report_hash=_hash(unsigned),
        cooldown_boundaries_covered=cooldown_covered,
        budget_boundaries_covered=budget_covered,
        loop_breaker_boundaries_covered=loop_covered,
    )


def validate_restart_guard_stress_report(value: Any) -> None:
    if not isinstance(value, RestartGuardStressReport):
        raise RestartGuardStressTestError("RESTART_GUARD_REPORT_TYPE_INVALID")
    if value.read_only is not True or any(
        (value.restart_authorized, value.service_control_authorized, value.execution_authorized)
    ):
        raise RestartGuardStressTestError("RESTART_GUARD_SAFETY_BOUNDARY_INVALID")
    unsigned = asdict(value)
    unsigned.pop("report_hash")
    unsigned["schema_version"] = SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if value.report_hash != _hash(unsigned):
        raise RestartGuardStressTestError("RESTART_GUARD_REPORT_HASH_MISMATCH")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
