from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .alert_severity_deduplication import (
    AlertDispositionResult,
    validate_alert_disposition_result,
)

POLICY_SCHEMA_VERSION = "phase4hp-alert-retry-backoff-policy-v1"
AttemptOutcome = Literal["FAILED", "DELIVERED"]
RetryStatus = Literal["READY", "WAIT", "EXHAUSTED", "DELIVERED", "DENIED", "INCOMPLETE"]


class AlertRetryBackoffPolicyError(ValueError):
    """Stable fail-closed alert retry and backoff policy error."""


@dataclass(frozen=True)
class AlertDeliveryAttempt:
    attempt_number: int
    attempted_at_epoch_seconds: int
    outcome: AttemptOutcome
    channel: str
    alert_result_hash: str
    complete: bool
    attempt_hash: str


@dataclass(frozen=True)
class AlertRetryDecision:
    status: RetryStatus
    reasons: tuple[str, ...]
    alert_result_hash: str
    attempt_count: int
    next_attempt_number: int | None
    next_eligible_at_epoch_seconds: int | None
    evaluated_at_epoch_seconds: int
    base_backoff_seconds: int
    max_backoff_seconds: int
    max_attempts: int
    attempts_hash: str
    decision_hash: str
    read_only: bool = True
    retry_candidate_ready: bool = False
    alert_delivery_authorized: bool = False
    notification_sent: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_alert_delivery_attempt(
    *,
    attempt_number: int,
    attempted_at_epoch_seconds: int,
    outcome: AttemptOutcome,
    channel: str,
    alert_result_hash: str,
    complete: bool,
) -> AlertDeliveryAttempt:
    unsigned = {
        "attempt_number": attempt_number,
        "attempted_at_epoch_seconds": attempted_at_epoch_seconds,
        "outcome": outcome,
        "channel": channel,
        "alert_result_hash": alert_result_hash,
        "complete": complete,
    }
    _validate_attempt_fields(unsigned)
    return AlertDeliveryAttempt(**unsigned, attempt_hash=_hash(unsigned))


def evaluate_alert_retry_backoff(
    alert_result: Any,
    attempts: Sequence[Any],
    *,
    evaluated_at_epoch_seconds: int,
    base_backoff_seconds: int = 30,
    max_backoff_seconds: int = 300,
    max_attempts: int = 4,
) -> AlertRetryDecision:
    for value in (
        evaluated_at_epoch_seconds,
        base_backoff_seconds,
        max_backoff_seconds,
        max_attempts,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AlertRetryBackoffPolicyError("POLICY_BOUND_INVALID")
    if max_backoff_seconds < base_backoff_seconds:
        raise AlertRetryBackoffPolicyError("POLICY_BOUND_INVALID")
    if not isinstance(alert_result, AlertDispositionResult):
        raise AlertRetryBackoffPolicyError("ALERT_RESULT_TYPE_INVALID")
    try:
        validate_alert_disposition_result(alert_result)
    except ValueError as exc:
        raise AlertRetryBackoffPolicyError("ALERT_RESULT_INVALID") from exc
    if len(attempts) > max_attempts:
        raise AlertRetryBackoffPolicyError("ATTEMPT_BOUND_EXCEEDED")
    ordered = [_validated_attempt(attempt) for attempt in attempts]
    ordered.sort(key=lambda attempt: attempt.attempt_number)
    for index, attempt in enumerate(ordered, start=1):
        if attempt.attempt_number != index:
            raise AlertRetryBackoffPolicyError("ATTEMPT_SEQUENCE_INVALID")
        if attempt.alert_result_hash != alert_result.result_hash:
            raise AlertRetryBackoffPolicyError("ATTEMPT_ALERT_BINDING_MISMATCH")
        if attempt.attempted_at_epoch_seconds > evaluated_at_epoch_seconds:
            raise AlertRetryBackoffPolicyError("ATTEMPT_FROM_FUTURE")
        if index > 1:
            previous_at = ordered[index - 2].attempted_at_epoch_seconds
            if attempt.attempted_at_epoch_seconds <= previous_at:
                raise AlertRetryBackoffPolicyError("ATTEMPT_TIME_NOT_MONOTONIC")
    delivered_indexes = [index for index, item in enumerate(ordered) if item.outcome == "DELIVERED"]
    if delivered_indexes and delivered_indexes[-1] != len(ordered) - 1:
        raise AlertRetryBackoffPolicyError("ATTEMPT_AFTER_DELIVERY")

    if alert_result.disposition != "EMIT" or not alert_result.alert_candidate_ready:
        status: RetryStatus = "DENIED"
        reasons = [f"ALERT_NOT_EMITTABLE:{alert_result.disposition}"]
        next_number = None
        next_eligible = None
    elif any(not attempt.complete for attempt in ordered):
        status = "INCOMPLETE"
        reasons = ["ALERT_ATTEMPT_HISTORY_INCOMPLETE"]
        next_number = None
        next_eligible = None
    elif delivered_indexes:
        status = "DELIVERED"
        reasons = []
        next_number = None
        next_eligible = None
    elif len(ordered) >= max_attempts:
        status = "EXHAUSTED"
        reasons = ["ALERT_RETRY_BUDGET_EXHAUSTED"]
        next_number = None
        next_eligible = None
    elif not ordered:
        status = "READY"
        reasons = []
        next_number = 1
        next_eligible = evaluated_at_epoch_seconds
    else:
        delay = min(base_backoff_seconds * (2 ** (len(ordered) - 1)), max_backoff_seconds)
        next_eligible = ordered[-1].attempted_at_epoch_seconds + delay
        next_number = len(ordered) + 1
        if evaluated_at_epoch_seconds >= next_eligible:
            status = "READY"
            reasons = []
        else:
            status = "WAIT"
            reasons = ["ALERT_BACKOFF_ACTIVE"]

    ready = status == "READY"
    attempts_payload = [asdict(attempt) for attempt in ordered]
    unsigned = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "alert_result_hash": alert_result.result_hash,
        "attempt_count": len(ordered),
        "next_attempt_number": next_number,
        "next_eligible_at_epoch_seconds": next_eligible,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "base_backoff_seconds": base_backoff_seconds,
        "max_backoff_seconds": max_backoff_seconds,
        "max_attempts": max_attempts,
        "attempts_hash": _hash(attempts_payload),
        "read_only": True,
        "retry_candidate_ready": ready,
        "alert_delivery_authorized": False,
        "notification_sent": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return AlertRetryDecision(
        status=status,
        reasons=tuple(reasons),
        alert_result_hash=alert_result.result_hash,
        attempt_count=len(ordered),
        next_attempt_number=next_number,
        next_eligible_at_epoch_seconds=next_eligible,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        base_backoff_seconds=base_backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
        max_attempts=max_attempts,
        attempts_hash=unsigned["attempts_hash"],
        decision_hash=_hash(unsigned),
        retry_candidate_ready=ready,
    )


def validate_alert_retry_decision(decision: Any) -> None:
    if not isinstance(decision, AlertRetryDecision):
        raise AlertRetryBackoffPolicyError("DECISION_TYPE_INVALID")
    if decision.read_only is not True or any(
        (
            decision.alert_delivery_authorized,
            decision.notification_sent,
            decision.recovery_authorized,
            decision.service_control_authorized,
            decision.host_restart_authorized,
            decision.execution_authorized,
        )
    ):
        raise AlertRetryBackoffPolicyError("DECISION_SAFETY_BOUNDARY_INVALID")
    ready = not decision.reasons and decision.retry_candidate_ready
    if (decision.status == "READY") != ready:
        raise AlertRetryBackoffPolicyError("DECISION_STATUS_INVALID")
    unsigned = asdict(decision)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = POLICY_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if decision.decision_hash != _hash(unsigned):
        raise AlertRetryBackoffPolicyError("DECISION_HASH_MISMATCH")


def _validated_attempt(value: Any) -> AlertDeliveryAttempt:
    if not isinstance(value, AlertDeliveryAttempt):
        raise AlertRetryBackoffPolicyError("ATTEMPT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("attempt_hash")
    _validate_attempt_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise AlertRetryBackoffPolicyError("ATTEMPT_HASH_MISMATCH")
    return value


def _validate_attempt_fields(payload: dict[str, Any]) -> None:
    for key in ("attempt_number", "attempted_at_epoch_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AlertRetryBackoffPolicyError("ATTEMPT_FIELD_INVALID")
    if payload["outcome"] not in {"FAILED", "DELIVERED"}:
        raise AlertRetryBackoffPolicyError("ATTEMPT_FIELD_INVALID")
    for key in ("channel", "alert_result_hash"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise AlertRetryBackoffPolicyError("ATTEMPT_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise AlertRetryBackoffPolicyError("ATTEMPT_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
