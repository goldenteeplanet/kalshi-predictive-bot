from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

EXPORT_SCHEMA_VERSION = "phase4ht-alert-delivery-audit-export-v1"
DeliveryOutcome = Literal["DELIVERED", "FAILED", "SUPPRESSED", "ACKNOWLEDGED"]


class AlertDeliveryAuditExportError(ValueError):
    """Stable fail-closed alert delivery audit export error."""


@dataclass(frozen=True)
class AlertDeliveryAuditRecord:
    event_id_hash: str
    incident_id_hash: str
    decision_hash: str
    occurred_at_epoch_seconds: int
    channel_code: str
    outcome: DeliveryOutcome
    complete: bool
    record_hash: str


@dataclass(frozen=True)
class AlertDeliveryAuditBundle:
    record_count: int
    window_start_epoch_seconds: int
    window_end_epoch_seconds: int
    records_hash: str
    canonical_json: str
    export_hash: str
    read_only: bool = True
    identifiers_redacted: bool = True
    alert_delivery_authorized: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_alert_delivery_audit_record(**fields: Any) -> AlertDeliveryAuditRecord:
    _validate_fields(fields)
    return AlertDeliveryAuditRecord(**fields, record_hash=_hash(fields))


def export_alert_delivery_audit(
    records: Sequence[Any],
    *,
    window_start_epoch_seconds: int,
    window_end_epoch_seconds: int,
    max_records: int = 1_000,
) -> AlertDeliveryAuditBundle:
    for value in (window_start_epoch_seconds, window_end_epoch_seconds, max_records):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AlertDeliveryAuditExportError("EXPORT_BOUND_INVALID")
    if max_records == 0 or window_start_epoch_seconds > window_end_epoch_seconds:
        raise AlertDeliveryAuditExportError("EXPORT_BOUND_INVALID")
    if isinstance(records, str | bytes) or len(records) > max_records:
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_BOUND_EXCEEDED")
    items = [_validated_record(item) for item in records]
    if len({item.event_id_hash for item in items}) != len(items):
        raise AlertDeliveryAuditExportError("EXPORT_EVENT_DUPLICATE")
    items.sort(key=lambda item: (item.occurred_at_epoch_seconds, item.event_id_hash))
    if any(not item.complete for item in items):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_INCOMPLETE")
    if any(
        not window_start_epoch_seconds <= item.occurred_at_epoch_seconds <= window_end_epoch_seconds
        for item in items
    ):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_OUTSIDE_WINDOW")
    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "record_count": len(items),
        "window_start_epoch_seconds": window_start_epoch_seconds,
        "window_end_epoch_seconds": window_end_epoch_seconds,
        "records": [asdict(item) for item in items],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return AlertDeliveryAuditBundle(
        record_count=len(items),
        window_start_epoch_seconds=window_start_epoch_seconds,
        window_end_epoch_seconds=window_end_epoch_seconds,
        records_hash=_hash(payload["records"]),
        canonical_json=canonical,
        export_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def validate_alert_delivery_audit_bundle(bundle: Any) -> None:
    if not isinstance(bundle, AlertDeliveryAuditBundle):
        raise AlertDeliveryAuditExportError("EXPORT_TYPE_INVALID")
    if (
        bundle.read_only is not True
        or bundle.identifiers_redacted is not True
        or any(
            (
                bundle.alert_delivery_authorized,
                bundle.recovery_authorized,
                bundle.service_control_authorized,
                bundle.host_restart_authorized,
                bundle.execution_authorized,
            )
        )
    ):
        raise AlertDeliveryAuditExportError("EXPORT_SAFETY_BOUNDARY_INVALID")
    try:
        payload = json.loads(bundle.canonical_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AlertDeliveryAuditExportError("EXPORT_JSON_INVALID") from exc
    if payload.get("schema_version") != EXPORT_SCHEMA_VERSION:
        raise AlertDeliveryAuditExportError("EXPORT_SCHEMA_INVALID")
    if (
        payload.get("record_count") != bundle.record_count
        or _hash(payload.get("records")) != bundle.records_hash
    ):
        raise AlertDeliveryAuditExportError("EXPORT_CONTENT_MISMATCH")
    if hashlib.sha256(bundle.canonical_json.encode()).hexdigest() != bundle.export_hash:
        raise AlertDeliveryAuditExportError("EXPORT_HASH_MISMATCH")


def _validated_record(value: Any) -> AlertDeliveryAuditRecord:
    if not isinstance(value, AlertDeliveryAuditRecord):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_TYPE_INVALID")
    fields = asdict(value)
    supplied = fields.pop("record_hash")
    _validate_fields(fields)
    if supplied != _hash(fields):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "event_id_hash",
        "incident_id_hash",
        "decision_hash",
        "occurred_at_epoch_seconds",
        "channel_code",
        "outcome",
        "complete",
    }
    if set(fields) != required:
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_FIELD_INVALID")
    if any(
        not _is_hash(fields[key]) for key in ("event_id_hash", "incident_id_hash", "decision_hash")
    ):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_FIELD_INVALID")
    if (
        isinstance(fields["occurred_at_epoch_seconds"], bool)
        or not isinstance(fields["occurred_at_epoch_seconds"], int)
        or fields["occurred_at_epoch_seconds"] < 0
    ):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_FIELD_INVALID")
    if not isinstance(fields["channel_code"], str) or not re.fullmatch(
        r"[A-Z0-9_]{1,32}", fields["channel_code"]
    ):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_FIELD_INVALID")
    if fields["outcome"] not in {
        "DELIVERED",
        "FAILED",
        "SUPPRESSED",
        "ACKNOWLEDGED",
    } or not isinstance(fields["complete"], bool):
        raise AlertDeliveryAuditExportError("EXPORT_RECORD_FIELD_INVALID")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
