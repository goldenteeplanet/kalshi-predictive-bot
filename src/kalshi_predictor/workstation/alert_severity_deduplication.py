from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

POLICY_SCHEMA_VERSION = "phase4ho-alert-severity-deduplication-v1"
Severity = Literal["INFO", "WARNING", "CRITICAL"]
Disposition = Literal["EMIT", "SUPPRESS_DUPLICATE", "STALE", "INCOMPLETE", "DENY"]

CRITICAL_CATEGORIES = {"PROTECTED_INVARIANT", "WRITER_EXCLUSIVITY", "POST_BOOT_FAILURE"}
WARNING_CATEGORIES = {
    "WSL_LIVENESS",
    "KEEPALIVE",
    "USER_SYSTEMD",
    "SCHEDULER",
    "DATABASE_READABILITY",
    "DISK_EXHAUSTION",
    "CLOCK_SKEW",
}
INFO_CATEGORIES = {"RECOVERY_SUCCEEDED", "RESTART_CANCELLED", "POST_BOOT_VERIFIED"}


class AlertSeverityDeduplicationError(ValueError):
    """Stable fail-closed alert severity and deduplication error."""


@dataclass(frozen=True)
class AlertCandidate:
    incident_id: str
    category: str
    reason_code: str
    observed_at_epoch_seconds: int
    evidence_age_seconds: int
    evidence_hash: str
    complete: bool
    source_identity_hash: str
    candidate_hash: str


@dataclass(frozen=True)
class PriorAlertRecord:
    record_id: str
    emitted_at_epoch_seconds: int
    deduplication_key_hash: str
    severity: Severity
    resolved: bool
    record_hash: str


@dataclass(frozen=True)
class AlertDispositionResult:
    disposition: Disposition
    severity: Severity | None
    reasons: tuple[str, ...]
    deduplication_key_hash: str
    candidate_hash: str
    matching_record_hash: str | None
    evaluated_at_epoch_seconds: int
    evidence_age_seconds: int
    max_evidence_age_seconds: int
    deduplication_window_seconds: int
    history_count: int
    history_hash: str
    result_hash: str
    read_only: bool = True
    alert_candidate_ready: bool = False
    alert_delivery_authorized: bool = False
    notification_sent: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_alert_candidate(
    *,
    incident_id: str,
    category: str,
    reason_code: str,
    observed_at_epoch_seconds: int,
    evidence_age_seconds: int,
    evidence_hash: str,
    complete: bool,
    source_identity_hash: str,
) -> AlertCandidate:
    unsigned = {
        "incident_id": incident_id,
        "category": category,
        "reason_code": reason_code,
        "observed_at_epoch_seconds": observed_at_epoch_seconds,
        "evidence_age_seconds": evidence_age_seconds,
        "evidence_hash": evidence_hash,
        "complete": complete,
        "source_identity_hash": source_identity_hash,
    }
    _validate_candidate_fields(unsigned)
    return AlertCandidate(**unsigned, candidate_hash=_hash(unsigned))


def make_prior_alert_record(
    *,
    record_id: str,
    emitted_at_epoch_seconds: int,
    deduplication_key_hash: str,
    severity: Severity,
    resolved: bool,
) -> PriorAlertRecord:
    unsigned = {
        "record_id": record_id,
        "emitted_at_epoch_seconds": emitted_at_epoch_seconds,
        "deduplication_key_hash": deduplication_key_hash,
        "severity": severity,
        "resolved": resolved,
    }
    _validate_record_fields(unsigned)
    return PriorAlertRecord(**unsigned, record_hash=_hash(unsigned))


def evaluate_alert_severity_and_deduplication(
    candidate: Any,
    history: Sequence[Any],
    *,
    evaluated_at_epoch_seconds: int,
    deduplication_window_seconds: int = 300,
    max_evidence_age_seconds: int = 120,
    max_history_records: int = 256,
) -> AlertDispositionResult:
    for value in (
        evaluated_at_epoch_seconds,
        deduplication_window_seconds,
        max_evidence_age_seconds,
        max_history_records,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AlertSeverityDeduplicationError("POLICY_BOUND_INVALID")
    if deduplication_window_seconds == 0 or max_history_records == 0:
        raise AlertSeverityDeduplicationError("POLICY_BOUND_INVALID")
    item = _validated_candidate(candidate)
    if len(history) > max_history_records:
        raise AlertSeverityDeduplicationError("HISTORY_BOUND_EXCEEDED")
    records = [_validated_record(record) for record in history]
    if len({record.record_id for record in records}) != len(records):
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_ID_DUPLICATE")
    records.sort(key=lambda record: (record.emitted_at_epoch_seconds, record.record_id))
    if any(record.emitted_at_epoch_seconds > evaluated_at_epoch_seconds for record in records):
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_FROM_FUTURE")

    severity = _severity_for(item.category)
    deduplication_key_hash = _hash(
        {
            "incident_id": item.incident_id,
            "category": item.category,
            "reason_code": item.reason_code,
        }
    )
    matching = [
        record
        for record in records
        if record.deduplication_key_hash == deduplication_key_hash and not record.resolved
    ]
    latest = matching[-1] if matching else None
    if item.evidence_age_seconds > max_evidence_age_seconds:
        disposition: Disposition = "STALE"
        reasons = ["ALERT_EVIDENCE_STALE"]
    elif not item.complete:
        disposition = "INCOMPLETE"
        reasons = ["ALERT_CANDIDATE_INCOMPLETE"]
    elif severity is None:
        disposition = "DENY"
        reasons = ["ALERT_CATEGORY_UNKNOWN"]
    elif item.observed_at_epoch_seconds > evaluated_at_epoch_seconds:
        disposition = "DENY"
        reasons = ["ALERT_CANDIDATE_FROM_FUTURE"]
    elif latest is not None and _severity_rank(severity) <= _severity_rank(latest.severity) and (
        evaluated_at_epoch_seconds - latest.emitted_at_epoch_seconds
        < deduplication_window_seconds
    ):
        disposition = "SUPPRESS_DUPLICATE"
        reasons = ["ALERT_DUPLICATE_WITHIN_WINDOW"]
    else:
        disposition = "EMIT"
        reasons = []

    ready = disposition == "EMIT"
    history_payload = [asdict(record) for record in records]
    unsigned = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "disposition": disposition,
        "severity": severity,
        "reasons": reasons,
        "deduplication_key_hash": deduplication_key_hash,
        "candidate_hash": item.candidate_hash,
        "matching_record_hash": latest.record_hash if latest else None,
        "evaluated_at_epoch_seconds": evaluated_at_epoch_seconds,
        "evidence_age_seconds": item.evidence_age_seconds,
        "max_evidence_age_seconds": max_evidence_age_seconds,
        "deduplication_window_seconds": deduplication_window_seconds,
        "history_count": len(records),
        "history_hash": _hash(history_payload),
        "read_only": True,
        "alert_candidate_ready": ready,
        "alert_delivery_authorized": False,
        "notification_sent": False,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return AlertDispositionResult(
        disposition=disposition,
        severity=severity,
        reasons=tuple(reasons),
        deduplication_key_hash=deduplication_key_hash,
        candidate_hash=item.candidate_hash,
        matching_record_hash=latest.record_hash if latest else None,
        evaluated_at_epoch_seconds=evaluated_at_epoch_seconds,
        evidence_age_seconds=item.evidence_age_seconds,
        max_evidence_age_seconds=max_evidence_age_seconds,
        deduplication_window_seconds=deduplication_window_seconds,
        history_count=len(records),
        history_hash=unsigned["history_hash"],
        result_hash=_hash(unsigned),
        alert_candidate_ready=ready,
    )


def validate_alert_disposition_result(result: Any) -> None:
    if not isinstance(result, AlertDispositionResult):
        raise AlertSeverityDeduplicationError("RESULT_TYPE_INVALID")
    if result.read_only is not True or any(
        (
            result.alert_delivery_authorized,
            result.notification_sent,
            result.recovery_authorized,
            result.service_control_authorized,
            result.host_restart_authorized,
            result.execution_authorized,
        )
    ):
        raise AlertSeverityDeduplicationError("RESULT_SAFETY_BOUNDARY_INVALID")
    emit = not result.reasons and result.alert_candidate_ready and result.severity is not None
    if (result.disposition == "EMIT") != emit:
        raise AlertSeverityDeduplicationError("RESULT_DISPOSITION_INVALID")
    unsigned = asdict(result)
    unsigned.pop("result_hash")
    unsigned["schema_version"] = POLICY_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    if result.result_hash != _hash(unsigned):
        raise AlertSeverityDeduplicationError("RESULT_HASH_MISMATCH")


def _validated_candidate(value: Any) -> AlertCandidate:
    if not isinstance(value, AlertCandidate):
        raise AlertSeverityDeduplicationError("CANDIDATE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("candidate_hash")
    _validate_candidate_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise AlertSeverityDeduplicationError("CANDIDATE_HASH_MISMATCH")
    return value


def _validated_record(value: Any) -> PriorAlertRecord:
    if not isinstance(value, PriorAlertRecord):
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_TYPE_INVALID")
    unsigned = asdict(value)
    supplied_hash = unsigned.pop("record_hash")
    _validate_record_fields(unsigned)
    if supplied_hash != _hash(unsigned):
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_HASH_MISMATCH")
    return value


def _validate_candidate_fields(payload: dict[str, Any]) -> None:
    for key in ("incident_id", "category", "reason_code"):
        value = payload[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise AlertSeverityDeduplicationError("CANDIDATE_FIELD_INVALID")
    for key in ("evidence_hash", "source_identity_hash"):
        if not _is_sha256(payload[key]):
            raise AlertSeverityDeduplicationError("CANDIDATE_FIELD_INVALID")
    for key in ("observed_at_epoch_seconds", "evidence_age_seconds"):
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AlertSeverityDeduplicationError("CANDIDATE_FIELD_INVALID")
    if not isinstance(payload["complete"], bool):
        raise AlertSeverityDeduplicationError("CANDIDATE_FIELD_INVALID")


def _validate_record_fields(payload: dict[str, Any]) -> None:
    if not isinstance(payload["record_id"], str) or not payload["record_id"].strip():
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_FIELD_INVALID")
    emitted_at = payload["emitted_at_epoch_seconds"]
    if isinstance(emitted_at, bool) or not isinstance(emitted_at, int) or emitted_at < 0:
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_FIELD_INVALID")
    if not _is_sha256(payload["deduplication_key_hash"]):
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_FIELD_INVALID")
    if payload["severity"] not in {"INFO", "WARNING", "CRITICAL"}:
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_FIELD_INVALID")
    if not isinstance(payload["resolved"], bool):
        raise AlertSeverityDeduplicationError("HISTORY_RECORD_FIELD_INVALID")


def _severity_for(category: str) -> Severity | None:
    if category in CRITICAL_CATEGORIES:
        return "CRITICAL"
    if category in WARNING_CATEGORIES:
        return "WARNING"
    if category in INFO_CATEGORIES:
        return "INFO"
    return None


def _severity_rank(severity: Severity) -> int:
    return {"INFO": 1, "WARNING": 2, "CRITICAL": 3}[severity]


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
