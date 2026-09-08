from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

CAPTURE_SCHEMA_VERSION = "phase4ij-scheduler-journal-evidence-capture-v1"
CaptureStatus = Literal["CAPTURED", "PARTIAL", "TAMPERED", "REFUSED"]


class SchedulerJournalEvidenceCaptureError(ValueError):
    """Stable fail-closed scheduler journal evidence capture error."""


@dataclass(frozen=True)
class SchedulerJournalEntryEvidence:
    cursor_hash: str
    unit_id_hash: str
    occurred_at_epoch_seconds: int
    priority: int
    message_hash: str
    byte_count: int
    complete: bool
    entry_hash: str


@dataclass(frozen=True)
class SchedulerJournalCapture:
    status: CaptureStatus
    reasons: tuple[str, ...]
    window_start_epoch_seconds: int
    window_end_epoch_seconds: int
    entry_count: int
    total_bytes: int
    warning_or_higher_count: int
    entry_hashes: tuple[str, ...]
    capture_hash: str
    read_only: bool = True
    messages_redacted: bool = True
    raw_output_retained: bool = False
    scheduler_journal_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_scheduler_journal_entry_evidence(**fields: Any) -> SchedulerJournalEntryEvidence:
    _validate_fields(fields)
    return SchedulerJournalEntryEvidence(**fields, entry_hash=_hash(fields))


def capture_scheduler_journal_evidence(
    entries: Sequence[Any],
    *,
    window_start_epoch_seconds: int,
    window_end_epoch_seconds: int,
    max_entries: int = 256,
    max_total_bytes: int = 262_144,
) -> SchedulerJournalCapture:
    for value in (
        window_start_epoch_seconds,
        window_end_epoch_seconds,
        max_entries,
        max_total_bytes,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SchedulerJournalEvidenceCaptureError("JOURNAL_BOUND_INVALID")
    if (
        window_start_epoch_seconds > window_end_epoch_seconds
        or max_entries == 0
        or max_total_bytes == 0
    ):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_BOUND_INVALID")
    if isinstance(entries, str | bytes) or len(entries) > max_entries:
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_ENTRY_BOUND_EXCEEDED")
    records = [_validated_entry(item) for item in entries]
    records.sort(key=lambda item: (item.occurred_at_epoch_seconds, item.cursor_hash))
    cursors = [item.cursor_hash for item in records]
    outside = any(
        not window_start_epoch_seconds <= item.occurred_at_epoch_seconds <= window_end_epoch_seconds
        for item in records
    )
    total_bytes = sum(item.byte_count for item in records)
    if len(set(cursors)) != len(cursors):
        status: CaptureStatus = "TAMPERED"
        reasons = ["JOURNAL_CURSOR_DUPLICATE"]
    elif outside:
        status = "REFUSED"
        reasons = ["JOURNAL_ENTRY_OUTSIDE_WINDOW"]
    elif total_bytes > max_total_bytes:
        status = "REFUSED"
        reasons = ["JOURNAL_TOTAL_BYTE_BOUND_EXCEEDED"]
    elif any(not item.complete for item in records):
        status = "PARTIAL"
        reasons = ["JOURNAL_ENTRY_INCOMPLETE"]
    else:
        status = "CAPTURED"
        reasons = []
    complete = status == "CAPTURED"
    hashes = tuple(item.entry_hash for item in records)
    warning_count = sum(item.priority <= 4 for item in records)
    unsigned = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "window_start_epoch_seconds": window_start_epoch_seconds,
        "window_end_epoch_seconds": window_end_epoch_seconds,
        "entry_count": len(records),
        "total_bytes": total_bytes,
        "warning_or_higher_count": warning_count,
        "entry_hashes": list(hashes),
        "read_only": True,
        "messages_redacted": True,
        "raw_output_retained": False,
        "scheduler_journal_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return SchedulerJournalCapture(
        status=status,
        reasons=tuple(reasons),
        window_start_epoch_seconds=window_start_epoch_seconds,
        window_end_epoch_seconds=window_end_epoch_seconds,
        entry_count=len(records),
        total_bytes=total_bytes,
        warning_or_higher_count=warning_count,
        entry_hashes=hashes,
        capture_hash=_hash(unsigned),
        scheduler_journal_complete=complete,
    )


def validate_scheduler_journal_capture(value: Any) -> None:
    if not isinstance(value, SchedulerJournalCapture):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_CAPTURE_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.messages_redacted is not True
        or value.raw_output_retained is not False
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_SAFETY_BOUNDARY_INVALID")
    if value.scheduler_journal_complete != (value.status == "CAPTURED"):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("capture_hash")
    unsigned["schema_version"] = CAPTURE_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["entry_hashes"] = list(unsigned["entry_hashes"])
    if value.capture_hash != _hash(unsigned):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_CAPTURE_HASH_MISMATCH")


def _validated_entry(value: Any) -> SchedulerJournalEntryEvidence:
    if not isinstance(value, SchedulerJournalEntryEvidence):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_ENTRY_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("entry_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_ENTRY_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "cursor_hash",
        "unit_id_hash",
        "occurred_at_epoch_seconds",
        "priority",
        "message_hash",
        "byte_count",
        "complete",
    }
    if set(fields) != required:
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_ENTRY_FIELD_INVALID")
    for key in ("cursor_hash", "unit_id_hash", "message_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise SchedulerJournalEvidenceCaptureError("JOURNAL_ENTRY_FIELD_INVALID")
    for key in ("occurred_at_epoch_seconds", "priority", "byte_count"):
        item = fields[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise SchedulerJournalEvidenceCaptureError("JOURNAL_ENTRY_FIELD_INVALID")
    if fields["priority"] > 7 or not isinstance(fields["complete"], bool):
        raise SchedulerJournalEvidenceCaptureError("JOURNAL_ENTRY_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
