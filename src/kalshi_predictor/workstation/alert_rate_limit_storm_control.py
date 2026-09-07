from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from .alert_retry_backoff_policy import AlertRetryDecision, validate_alert_retry_decision

POLICY_SCHEMA_VERSION = "phase4hq-alert-rate-limit-storm-control-v1"
Severity = Literal["INFO", "WARNING", "CRITICAL"]
AdmissionStatus = Literal["ALLOW", "RATE_LIMITED", "STORM_SUPPRESSED", "DENIED", "INCOMPLETE"]


class AlertRateLimitStormControlError(ValueError):
    """Stable fail-closed alert rate-limit and storm-control error."""


@dataclass(frozen=True)
class AlertEmissionEvent:
    event_id: str
    emitted_at_epoch_seconds: int
    severity: Severity
    alert_result_hash: str
    complete: bool
    event_hash: str


@dataclass(frozen=True)
class AlertAdmissionDecision:
    status: AdmissionStatus
    reasons: tuple[str, ...]
    severity: Severity
    retry_decision_hash: str
    evaluated_at_epoch_seconds: int
    short_window_seconds: int
    short_window_limit: int
    noncritical_limit: int
    short_window_count: int
    short_window_noncritical_count: int
    storm_window_seconds: int
    storm_limit: int
    storm_window_count: int
    history_count: int
    history_hash: str
    decision_hash: str
    read_only: bool = True
    admission_candidate_ready: bool = False
    alert_delivery_authorized: bool = False
    notification_sent: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_alert_emission_event(
    *,
    event_id: str,
    emitted_at_epoch_seconds: int,
    severity: Severity,
    alert_result_hash: str,
    complete: bool,
) -> AlertEmissionEvent:
    unsigned = {
        "event_id": event_id,
        "emitted_at_epoch_seconds": emitted_at_epoch_seconds,
        "severity": severity,
        "alert_result_hash": alert_result_hash,
        "complete": complete,
    }
    _validate_event_fields(unsigned)
    return AlertEmissionEvent(**unsigned, event_hash=_hash(unsigned))


def evaluate_alert_rate_limit_and_storm_control(
    retry_decision: Any,
    severity: Severity,
    history: Sequence[Any],
    *,
    evaluated_at_epoch_seconds: int,
    short_window_seconds: int = 60,
    short_window_limit: int = 5,
    noncritical_limit: int = 3,
    storm_window_seconds: int = 300,
    storm_limit: int = 20,
    max_history_records: int = 512,
) -> AlertAdmissionDecision:
    for value in (
        evaluated_at_epoch_seconds,
        short_window_seconds,
        short_window_limit,
        noncritical_limit,
        storm_window_seconds,
        storm_limit,
        max_history_records,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AlertRateLimitStormControlError("POLICY_BOUND_INVALID")
    if noncritical_limit > short_window_limit or short_window_seconds > storm_window_seconds:
        raise AlertRateLimitStormControlError("POLICY_BOUND_INVALID")
    if severity not in {"INFO", "WARNING", "CRITICAL"}:
        raise AlertRateLimitStormControlError("SEVERITY_INVALID")
    if not isinstance(retry_decision, AlertRetryDecision):
        raise AlertRateLimitStormControlError("RETRY_DECISION_TYPE_INVALID")
    try:
        validate_alert_retry_decision(retry_decision)
    except ValueError as exc:
        raise AlertRateLimitStormControlError("RETRY_DECISION_INVALID") from exc
    if len(history) > max_history_records:
        raise AlertRateLimitStormControlError("HISTORY_BOUND_EXCEEDED")
    records = [_validated_event(event) for event in history]
    if len({event.event_id for event in records}) != len(records):
        raise AlertRateLimitStormControlError("HISTORY_EVENT_ID_DUPLICATE")
    records.sort(key=lambda event: (event.emitted_at_epoch_seconds, event.event_id))
    if any(event.emitted_at_epoch_seconds > evaluated_at_epoch_seconds for event in records):
        raise AlertRateLimitStormControlError("HISTORY_EVENT_FROM_FUTURE")

    short_records = [
        event
        for event in records
        if evaluated_at_epoch_seconds - event.emitted_at_epoch_seconds < short_window_seconds
    ]
    storm_records = [
        event
        for event in records
        if evaluated_at_epoch_seconds - event.emitted_at_epoch_seconds < storm_window_seconds
    ]
    noncritical_count = sum(event.severity != "CRITICAL" for event in short_records)
    if retry_decision.status != "READY" or not retry_decision.retry_candidate_ready:
        status: AdmissionStatus = "DENIED"
        reasons = [f"ALERT_RETRY_NOT_READY:{retry_decision.status}"]
    elif any(not event.complete for event in records):
        status = "INCOMPLETE"
        reasons = ["ALERT_EMISSION_HISTORY_INCOMPLETE"]
    elif len(storm_records) >= storm_limit:
        status = "STORM_SUPPRESSED"
        reasons = ["ALERT_STORM_LIMIT_REACHED"]
    elif len(short_records) >= short_window_limit:
        status = "RATE_LIMITED"
        reasons = ["ALERT_SHORT_WINDOW_LIMIT_REACHED"]
    elif severity != "CRITICAL" and noncritical_count >= noncritical_limit:
        status = "RATE_LIMITED"
        reasons = ["ALERT_NONCRITICAL_CAPACITY_RESERVED"]
    else:
        status = "ALLOW"
        reasons = []

    ready = status == "ALLOW"
    history_payload = [asdict(event) for event in records]
    unsigned = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "severity": severity,
        "retry_decision_hash": retry_decision.decision_hash,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "short_window_seconds": short_window_seconds,
        "short_window_limit": short_window_limit,
        "noncritical_limit": noncritical_limit,
        "short_window_count": len(short_records),
        "short_window_noncritical_count": noncritical_count,
        "storm_window_seconds": storm_window_seconds,
        "storm_limit": storm_limit,
        "storm_window_count": len(storm_records),
        "history_count": len(records),
        "history_hash": _hash(history_payload),
        "read_only": True,
        "admission_candidate_ready": ready,
        "alert_delivery_authorized": False,
        "notification_sent": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return AlertAdmissionDecision(
        status=status,
        reasons=tuple(reasons),
        severity=severity,
        retry_decision_hash=retry_decision.decision_hash,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        short_window_seconds=short_window_seconds,
        short_window_limit=short_window_limit,
        noncritical_limit=noncritical_limit,
        short_window_count=len(short_records),
        short_window_noncritical_count=noncritical_count,
        storm_window_seconds=storm_window_seconds,
        storm_limit=storm_limit,
        storm_window_count=len(storm_records),
        history_count=len(records),
        history_hash=unsigned["history_hash"],
        decision_hash=_hash(unsigned),
        admission_candidate_ready=ready,
    )


def validate_alert_admission_decision(decision: Any) -> None:
    if not isinstance(decision, AlertAdmissionDecision):
        raise AlertRateLimitStormControlError("DECISION_TYPE_INVALID")
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
        raise AlertRateLimitStormControlError("DECISION_SAFETY_BOUNDARY_INVALID")
    allowed = not decision.reasons and decision.admission_candidate_ready
    if (decision.status == "ALLOW") != allowed:
        raise AlertRateLimitStormControlError("DECISION_STATUS_INVALID")
    unsigned = asdict(decision)
    unsigned.pop("decision_hash")
    unsigned["schema_version"] = POLICY_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if decision.decision_hash != _hash(unsigned):
        raise AlertRateLimitStormControlError("DECISION_HASH_MISMATCH")


def _validated_event(value: Any) -> AlertEmissionEvent:
    if not isinstance(value, AlertEmissionEvent):
        raise AlertRateLimitStormControlError("HISTORY_EVENT_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("event_hash")
    _validate_event_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise AlertRateLimitStormControlError("HISTORY_EVENT_HASH_MISMATCH")
    return value


def _validate_event_fields(payload: dict[str, Any]) -> None:
    if not isinstance(payload["event_id"], str) or not payload["event_id"].strip():
        raise AlertRateLimitStormControlError("HISTORY_EVENT_FIELD_INVALID")
    emitted_at = payload["emitted_at_epoch_seconds"]
    if isinstance(emitted_at, bool) or not isinstance(emitted_at, int) or emitted_at < 0:
        raise AlertRateLimitStormControlError("HISTORY_EVENT_FIELD_INVALID")
    if payload["severity"] not in {"INFO", "WARNING", "CRITICAL"}:
        raise AlertRateLimitStormControlError("HISTORY_EVENT_FIELD_INVALID")
    if (
        not isinstance(payload["alert_result_hash"], str)
        or not payload["alert_result_hash"].strip()
    ):
        raise AlertRateLimitStormControlError("HISTORY_EVENT_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise AlertRateLimitStormControlError("HISTORY_EVENT_FIELD_INVALID")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
