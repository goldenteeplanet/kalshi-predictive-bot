from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

COLLECTOR_SCHEMA_VERSION = "phase4if-bounded-diagnostics-collector-v1"
CollectionStatus = Literal["COLLECTED", "PARTIAL", "REFUSED", "TAMPERED"]


class BoundedDiagnosticsCollectorError(ValueError):
    """Stable fail-closed bounded diagnostics collector error."""


@dataclass(frozen=True)
class DiagnosticSample:
    sample_id_hash: str
    source_code: str
    captured_at_epoch_seconds: int
    content_hash: str
    byte_count: int
    line_count: int
    truncated: bool
    complete: bool
    sample_hash: str


@dataclass(frozen=True)
class DiagnosticCollection:
    status: CollectionStatus
    reasons: tuple[str, ...]
    window_start_epoch_seconds: int
    window_end_epoch_seconds: int
    sample_count: int
    total_bytes: int
    total_lines: int
    sample_hashes: tuple[str, ...]
    collection_hash: str
    read_only: bool = True
    raw_content_retained: bool = False
    identifiers_redacted: bool = True
    diagnostics_complete: bool = False
    recovery_authorized: bool = False
    service_control_authorized: bool = False
    host_restart_authorized: bool = False
    execution_authorized: bool = False


def make_diagnostic_sample(**fields: Any) -> DiagnosticSample:
    _validate_fields(fields)
    return DiagnosticSample(**fields, sample_hash=_hash(fields))


def collect_bounded_diagnostics(
    samples: Sequence[Any],
    *,
    window_start_epoch_seconds: int,
    window_end_epoch_seconds: int,
    max_records: int = 16,
    max_total_bytes: int = 262_144,
    max_total_lines: int = 5_000,
) -> DiagnosticCollection:
    for value in (
        window_start_epoch_seconds,
        window_end_epoch_seconds,
        max_records,
        max_total_bytes,
        max_total_lines,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_BOUND_INVALID")
    if (
        window_start_epoch_seconds > window_end_epoch_seconds
        or min(max_records, max_total_bytes, max_total_lines) == 0
    ):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_BOUND_INVALID")
    if isinstance(samples, (str, bytes)) or len(samples) > max_records:
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_RECORD_BOUND_EXCEEDED")
    records = [_validated_sample(item) for item in samples]
    records.sort(
        key=lambda item: (item.captured_at_epoch_seconds, item.source_code, item.sample_id_hash)
    )
    ids = [item.sample_id_hash for item in records]
    total_bytes = sum(item.byte_count for item in records)
    total_lines = sum(item.line_count for item in records)
    outside = any(
        not window_start_epoch_seconds <= item.captured_at_epoch_seconds <= window_end_epoch_seconds
        for item in records
    )
    if len(set(ids)) != len(ids):
        status: CollectionStatus = "TAMPERED"
        reasons = ["DIAGNOSTIC_SAMPLE_ID_DUPLICATE"]
    elif outside:
        status = "REFUSED"
        reasons = ["DIAGNOSTIC_SAMPLE_OUTSIDE_WINDOW"]
    elif total_bytes > max_total_bytes or total_lines > max_total_lines:
        status = "REFUSED"
        reasons = ["DIAGNOSTIC_TOTAL_BOUND_EXCEEDED"]
    elif any(not item.complete or item.truncated for item in records):
        status = "PARTIAL"
        reasons = ["DIAGNOSTIC_SAMPLE_PARTIAL_OR_TRUNCATED"]
    else:
        status = "COLLECTED"
        reasons = []
    complete = status == "COLLECTED"
    hashes = tuple(item.sample_hash for item in records)
    unsigned = {
        "schema_version": COLLECTOR_SCHEMA_VERSION,
        "status": status,
        "reasons": reasons,
        "window_start_epoch_seconds": window_start_epoch_seconds,
        "window_end_epoch_seconds": window_end_epoch_seconds,
        "sample_count": len(records),
        "total_bytes": total_bytes,
        "total_lines": total_lines,
        "sample_hashes": list(hashes),
        "read_only": True,
        "raw_content_retained": False,
        "identifiers_redacted": True,
        "diagnostics_complete": complete,
        "recovery_authorized": False,
        "service_control_authorized": False,
        "host_restart_authorized": False,
        "execution_authorized": False,
    }
    return DiagnosticCollection(
        status=status,
        reasons=tuple(reasons),
        window_start_epoch_seconds=window_start_epoch_seconds,
        window_end_epoch_seconds=window_end_epoch_seconds,
        sample_count=len(records),
        total_bytes=total_bytes,
        total_lines=total_lines,
        sample_hashes=hashes,
        collection_hash=_hash(unsigned),
        diagnostics_complete=complete,
    )


def validate_diagnostic_collection(value: Any) -> None:
    if not isinstance(value, DiagnosticCollection):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_COLLECTION_TYPE_INVALID")
    if (
        value.read_only is not True
        or value.raw_content_retained is not False
        or value.identifiers_redacted is not True
        or any(
            (
                value.recovery_authorized,
                value.service_control_authorized,
                value.host_restart_authorized,
                value.execution_authorized,
            )
        )
    ):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAFETY_BOUNDARY_INVALID")
    if value.diagnostics_complete != (value.status == "COLLECTED"):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_STATUS_INVALID")
    unsigned = asdict(value)
    unsigned.pop("collection_hash")
    unsigned["schema_version"] = COLLECTOR_SCHEMA_VERSION
    unsigned["reasons"] = list(unsigned["reasons"])
    unsigned["sample_hashes"] = list(unsigned["sample_hashes"])
    if value.collection_hash != _hash(unsigned):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_COLLECTION_HASH_MISMATCH")


def _validated_sample(value: Any) -> DiagnosticSample:
    if not isinstance(value, DiagnosticSample):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAMPLE_TYPE_INVALID")
    unsigned = asdict(value)
    supplied = unsigned.pop("sample_hash")
    _validate_fields(unsigned)
    if supplied != _hash(unsigned):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAMPLE_HASH_MISMATCH")
    return value


def _validate_fields(fields: dict[str, Any]) -> None:
    required = {
        "sample_id_hash",
        "source_code",
        "captured_at_epoch_seconds",
        "content_hash",
        "byte_count",
        "line_count",
        "truncated",
        "complete",
    }
    if set(fields) != required:
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAMPLE_FIELD_INVALID")
    for key in ("sample_id_hash", "content_hash"):
        if not isinstance(fields[key], str) or re.fullmatch(r"[0-9a-f]{64}", fields[key]) is None:
            raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAMPLE_FIELD_INVALID")
    if (
        not isinstance(fields["source_code"], str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", fields["source_code"]) is None
    ):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAMPLE_FIELD_INVALID")
    for key in ("captured_at_epoch_seconds", "byte_count", "line_count"):
        item = fields[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAMPLE_FIELD_INVALID")
    if not isinstance(fields["truncated"], bool) or not isinstance(fields["complete"], bool):
        raise BoundedDiagnosticsCollectorError("DIAGNOSTIC_SAMPLE_FIELD_INVALID")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
